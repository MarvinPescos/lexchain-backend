"""Finalize: the lawyer confirms the signed copy, and it becomes permanent.

Freeze the current signed copy, archive it (read back and checked), anchor the
SHA-256 of its bytes on-chain, then hand it to the OCR pipeline through a
durable dispatch. Moves to an `attestation/` feature once chain anchoring
becomes its own background job.
"""

import hashlib
import uuid
from datetime import datetime, timezone

from app.features.blockchain.schemas import RecordResponse
from app.features.snapshot import DocumentSnapshotService
from app.features.snapshot.backups import SnapshotArchive
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.infrastructure.backup import BackupProvider
from app.infrastructure.blockchain import BlockchainProvider
from app.infrastructure.clients.supabase_storage import get_storage_client
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.queue import DocumentQueue
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.errors.exceptions import ConflictError, ServiceUnavailableError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import lifecycle
from ..access import DocumentAccess
from ..constants import ACTION_FINALIZE, JOB_EXTRACT, STATUS_QUEUED
from ..dispatch import DocumentDispatcher
from ..repository import DocumentRepository


class DocumentFinalization:
    def __init__(
        self,
        session: AsyncSession,
        queue: DocumentQueue,
        provider: BlockchainProvider,
        backup: BackupProvider | None = None,
    ) -> None:
        self.session = session
        self.dispatcher = DocumentDispatcher(session, queue)
        self.repo = DocumentRepository(session)
        self.access = DocumentAccess(session)
        self.snapshot_service = DocumentSnapshotService(session)
        self.snapshot_repo = DocumentSnaphotRepository(session)
        # None when BACKUP_ENABLED is off: finalize then leaves no archive, and
        # the document is not recoverable until backup verification repairs it.
        self.archive = SnapshotArchive(session, backup) if backup else None
        from app.features.blockchain.service import BlockchainService

        self.blockchain_service = BlockchainService(session, provider)

    async def finalize(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> RecordResponse:
        """Freeze the signed copy, anchor it, then queue it for processing.

        What is anchored is the SHA-256 of the final PDF bytes. OCR, review and
        enrichment run afterwards on the frozen copy and never feed the chain.
        """
        user_id = user.user_id
        document = await self.access.authorize(user, document_id, ACTION_FINALIZE)

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
        if self.archive:
            snapshot = await self.snapshot_repo.get_by_document_id(document_id)
            # Nothing is anchored without a verified archive. The raise rolls
            # back the snapshot; the archive key is content-addressed, so a
            # retry checks and reuses an object an earlier attempt left.
            if not await self.archive.archive(snapshot, content):
                raise ServiceUnavailableError(
                    "The finalized copy could not be archived. Try finalizing again."
                )
        record = await self.blockchain_service.record_document(document, file_sha256)

        change = lifecycle.transition(ACTION_FINALIZE, document, user_id)
        document.finalized_by = user_id
        document.finalized_at = datetime.now(timezone.utc)
        await self.repo.add(change)
        # The OCR job is promised in the same commit as the finalization, so a
        # broker outage can delay processing but never lose it.
        dispatch = await self.dispatcher.record(document.id, JOB_EXTRACT)
        document.status = STATUS_QUEUED
        await self.session.commit()

        # Only now does the pipeline see the document.
        await self.dispatcher.send(dispatch)

        from app.celery_tasks import handle_document_recorded

        handle_document_recorded.delay(
            user_id=str(user_id),
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
