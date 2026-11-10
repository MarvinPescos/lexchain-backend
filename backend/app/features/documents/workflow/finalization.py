"""Finalize: the lawyer confirms the signed copy, and it becomes permanent.

Allowed only once the signed copy has been processed (COMPLETED), so the
lawyer has seen its risk clauses while it could still be replaced. Freeze the
current signed copy, archive it (read back and checked), and anchor the
SHA-256 of its bytes on-chain. The metadata and analysis records are archived
the same way and anchored last: the PDF anchor is the point of no return, so a
failed record anchor leaves the document finalized for the scheduled retry. With
no archive there are no record anchors either. Nothing is queued: processing already ran.
Moves to an `attestation/` feature once chain anchoring becomes its own
background job.
"""

import hashlib
import uuid
from datetime import datetime, timezone

from app.features.blockchain.schemas import RecordResponse
from app.features.snapshot import DocumentSnapshotService
from app.features.snapshot.backups import SnapshotArchive
from app.features.snapshot.records import (
    analysis_anchor_id,
    analysis_rows,
    canonical_json,
    metadata_anchor_id,
    metadata_record,
    sha256_hex,
)
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.infrastructure.backup import BackupProvider
from app.infrastructure.blockchain import BlockchainProvider
from app.infrastructure.clients.supabase_storage import get_storage_client
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.constants import PARTY_STATUS_ACCEPTED
from app.shared.errors.exceptions import ConflictError, ServiceUnavailableError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import lifecycle
from ..access import DocumentAccess
from ..constants import (
    ACTION_FINALIZE,
    STATUS_AWAITING_REVIEW,
    STATUS_COMPLETED,
    STATUS_FAILED,
)
from ..models import Document
from ..repository import DocumentRepository


def ensure_processed(document: Document) -> None:
    """Refuse to finalize a signed copy whose processing has not completed."""
    if document.status == STATUS_COMPLETED:
        return
    if document.status == STATUS_FAILED:
        message = (
            "Processing of the signed copy failed. Retry it, and finalize once "
            "its risk check is complete."
        )
    elif document.status == STATUS_AWAITING_REVIEW:
        message = (
            "Review and approve the extracted text first. Finalize once the "
            "risk check is complete."
        )
    else:
        message = (
            "The signed copy is still being processed. Finalize once its risk "
            "check is complete."
        )
    raise ConflictError(
        message,
        details={"reason": "PROCESSING_NOT_COMPLETE", "status": document.status},
    )


class DocumentFinalization:
    def __init__(
        self,
        session: AsyncSession,
        provider: BlockchainProvider,
        backup: BackupProvider | None = None,
    ) -> None:
        self.session = session
        self.repo = DocumentRepository(session)
        self.access = DocumentAccess(session)
        self.snapshot_service = DocumentSnapshotService(session)
        self.snapshot_repo = DocumentSnaphotRepository(session)
        # None when BACKUP_ENABLED is off: finalize then leaves no archive, and
        # the document is not recoverable until backup verification repairs it.
        self.archive = SnapshotArchive(session, backup) if backup else None
        self.provider = provider
        from app.features.blockchain.service import BlockchainService

        self.blockchain_service = BlockchainService(session, provider)

    async def finalize(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> RecordResponse:
        """Freeze the signed copy and anchor it. Refused until processing is COMPLETED.

        What is anchored as the document is the SHA-256 of the final PDF bytes.
        The approved text, chunks and insight are frozen beside it as the
        analysis record: processing is complete, so all of it exists now.
        """
        user_id = user.user_id
        document = await self.access.authorize(user, document_id, ACTION_FINALIZE)
        # Locked so a replace cannot swap the copy between this check and the
        # anchor; the replace waits, then finds the document FINALIZED.
        await self.session.refresh(document, with_for_update=True)
        lifecycle.ensure(ACTION_FINALIZE, document)
        ensure_processed(document)

        await self.session.refresh(document, ["signed_copy"])
        copy = document.signed_copy
        if copy is None:
            raise ConflictError("This document has no signed copy to finalize")

        # Re-read the stored bytes: the hash must describe what is stored now,
        # and must still be the file the lawyer uploaded and checked.
        content = await get_storage_client().download(copy.storage_url)
        file_sha256 = hashlib.sha256(content).hexdigest()
        if file_sha256 != copy.sha256:
            log.error(
                "document.finalize.stored_file_changed",
                document_id=str(document_id),
                expected=copy.sha256,
                actual=file_sha256,
            )
            raise ConflictError(
                "The stored signed copy no longer matches the file that was "
                "uploaded. Replace the signed copy before finalizing."
            )

        await self.snapshot_service.create_snapshot(
            document_id, copy.id, file_sha256, len(content)
        )
        # Set first: both are part of the metadata record.
        document.finalized_by = user_id
        document.finalized_at = datetime.now(timezone.utc)
        metadata: bytes | None = None
        analysis: bytes | None = None
        if self.archive:
            snapshot = await self.snapshot_repo.get_by_document_id(document_id)
            assert snapshot is not None
            # Nothing is anchored without a verified archive. The raise rolls
            # back the snapshot; the archive key is content-addressed, so a
            # retry checks and reuses an object an earlier attempt left.
            if not await self.archive.archive(snapshot, content):
                raise ServiceUnavailableError(
                    "The finalized copy could not be archived. Try finalizing again."
                )
            metadata = canonical_json(metadata_record(document))
            if not await self.archive.archive_metadata(snapshot, metadata):
                raise ServiceUnavailableError(
                    "The finalized record could not be archived. Try finalizing again."
                )
            analysis = canonical_json(
                (await analysis_rows(self.session, document_id)).record()
            )
            if not await self.archive.archive_analysis(snapshot, analysis):
                raise ServiceUnavailableError(
                    "The document's analysis could not be archived. Try finalizing again."
                )
        record = await self.blockchain_service.record_document(document, file_sha256)

        change = lifecycle.transition(ACTION_FINALIZE, document, user_id)
        await self.repo.add(change)
        await self.session.commit()

        if metadata is not None:
            await self._anchor_record(metadata_anchor_id(document_id), metadata)
        if analysis is not None:
            await self._anchor_record(analysis_anchor_id(document_id), analysis)

        from app.celery_tasks import handle_document_recorded

        # The lawyer and every client on the document hear it is permanent.
        parties = await self.repo.get_parties(document_id)
        recipients = {document.user_id, user_id} | {
            p.user_id
            for p in parties
            if p.status == PARTY_STATUS_ACCEPTED and p.user_id is not None
        }
        for recipient in recipients:
            handle_document_recorded.delay(
                user_id=str(recipient),
                document_title=document.file_name,
                document_id=str(document_id),
                tx_hash=record.tx_hash,
            )

        log.info(
            "document.finalize",
            user_id=str(user_id),
            document_id=str(document_id),
            tx_hash=record.tx_hash,
            file_sha256=file_sha256,
        )
        return record

    async def _anchor_record(self, anchor_id: str, record: bytes) -> None:
        """Never undoes finalize: `retry-metadata-anchors` picks up a failure."""
        try:
            await self.provider.store_document_hash(anchor_id, sha256_hex(record))
        except Exception as e:
            log.error(
                "document.finalize.record_anchor_failed",
                anchor_id=anchor_id,
                error=f"{type(e).__name__}: {e}",
            )
