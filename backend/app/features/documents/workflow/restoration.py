"""Restore: repair everything found wrong with a finalized document.

Every value comes from the chain and the archive written at finalize, never from
the database or the active file, which are what no longer match. Bytes are used
only once they re-hash to the on-chain hash, so an altered row can neither block
a restore nor choose what is restored. The file found in place of the original,
if any, is kept as its own signed copy. Nothing is re-anchored: the original
chain records are what the document verifies against. It stays a person's click,
never automatic.

Restored chunks have no embedding until `document.reembed_chunks` runs, queued
after the commit. Until then they are missing from vector search, which beats
searching tampered text.
"""

import hashlib
import uuid
from datetime import datetime
from typing import Any

import httpx
from storage3.utils import StorageException

from app.features.chunks.models import DocumentChunk
from app.features.documents.extraction.models import DocumentExtraction
from app.features.documents.schemas import RestoredValue, RestoreResponse
from app.features.insights.models import DocumentInsight
from app.features.snapshot.models import DocumentSnapshot
from app.features.snapshot.records import (
    CHUNK_FIELDS,
    INSIGHT_FIELDS,
    MetadataCheck,
    RecordCheck,
    analysis_rows,
    archive_key,
    document_hash_change,
)
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.features.snapshot.schemas import RecordChange
from app.features.users.models import User
from app.infrastructure.backup import BackupProvider
from app.infrastructure.blockchain import BlockchainProvider
from app.infrastructure.clients.supabase_storage import (
    get_storage_client,
    object_path_from_url,
)
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.errors.exceptions import ConflictError, ServiceUnavailableError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import lifecycle
from ..access import DocumentAccess
from ..constants import ACTION_RESTORE, LIFECYCLE_FINALIZED
from ..models import Document, SignedCopy
from ..repository import DocumentRepository

RESTORED_REASON = (
    "Restored from the archived original: the stored file no longer matched "
    "the hash anchored on-chain."
)
RESTORED_MISSING_REASON = "Restored from the archived original: the stored file was missing."

_STORAGE_ERRORS = (httpx.HTTPError, OSError, StorageException)
_UUID_FIELDS = ("book_id", "finalized_by", "user_id")


class DocumentRestoration:
    def __init__(
        self,
        session: AsyncSession,
        provider: BlockchainProvider,
        backup: BackupProvider | None = None,
    ) -> None:
        self.session = session
        self.repo = DocumentRepository(session)
        self.access = DocumentAccess(session)
        # None when BACKUP_ENABLED is off: there is no archive to restore from.
        self.backup = backup
        from app.features.blockchain.service import BlockchainService

        self.verifier = BlockchainService(session, provider, backup)
        self.records = RecordCheck(session, provider, backup)
        self.snapshot_repo = DocumentSnaphotRepository(session)

    async def restore(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> RestoreResponse:
        """Repair the lifecycle, snapshot, file, metadata and analysis, in that order.

        Every refusal happens before anything is written, and the repairs commit
        together. With nothing wrong, answers `restored=False`.
        """
        document = await self.access.authorize(user, document_id, ACTION_RESTORE)
        # One restore at a time: a concurrent retry waits, then finds it authentic.
        await self.session.refresh(document, with_for_update=True)

        # The chain is read before `lifecycle` is looked at: it may be the edit.
        verdict = await self.verifier.verify_document(
            user, document_id, tamper_report=False
        )
        if verdict.status == "NOT_ANCHORED":
            raise ConflictError("Only a finalized document can be restored.")
        if verdict.status not in (
            "AUTHENTIC",
            "TAMPERED",
            "FILE_MISSING",
            "SNAPSHOT_COMPROMISED",
        ):
            raise ServiceUnavailableError(
                "Could not read the stored file or the chain to confirm it was "
                "tampered with. Try again once both are available."
            )

        onchain_hash = verdict.onchain_hash
        assert onchain_hash is not None
        file_bad = verdict.status != "AUTHENTIC"
        snapshot = await self.snapshot_repo.get_by_document_id(document_id)
        assert snapshot is not None  # no verdict above is reached without one
        metadata = await self.records.metadata(document)
        if metadata.status == "UNAVAILABLE":
            self._refuse_record(metadata)
        analysis = await self.records.analysis(document)
        if analysis.status == "UNAVAILABLE":
            self._refuse_record(analysis, "analysis")

        snapshot_edited = snapshot.file_sha256 != onchain_hash
        original = await self._restore_source(
            document_id, onchain_hash, required=file_bad or snapshot_edited
        )
        snapshot_changes = await self.records.snapshot_changes(
            snapshot, onchain_hash, original
        )
        lifecycle_edited = document.lifecycle != LIFECYCLE_FINALIZED
        hash_change = document_hash_change(document, onchain_hash)
        if not (
            file_bad
            or lifecycle_edited
            or hash_change
            or snapshot_changes
            or metadata.changes
            or analysis.changes
        ):
            assert verdict.current_hash is not None
            assert document.signed_copy_id is not None
            return RestoreResponse(
                document_id=document_id,
                restored=False,
                restored_hash=verdict.current_hash,
                restored_copy_id=document.signed_copy_id,
                verification=verdict,
            )

        current = document.signed_copy
        if file_bad and current is None:
            raise ConflictError(
                "This document has no signed copy record to restore onto.",
                details={"reason": "NO_SIGNED_COPY"},
            )
        previous_hash = verdict.current_hash if file_bad else None  # None if deleted
        tampered = (
            await self._current_bytes(document, previous_hash)
            if previous_hash is not None
            else None
        )

        uploaded: list[str] = []
        repaired: list[RestoredValue] = []
        unrepaired: list[str] = []
        preserved = None
        try:
            from_stage = document.lifecycle
            change = lifecycle.transition(ACTION_RESTORE, document, user.user_id)
            if lifecycle_edited:
                repaired.append(
                    RestoredValue(
                        record="lifecycle",
                        field="lifecycle",
                        old=from_stage,
                        new=document.lifecycle,
                    )
                )
            if hash_change is not None:
                document.document_hash = onchain_hash
                repaired.append(
                    RestoredValue(
                        record="document",
                        field=hash_change.field,
                        old=hash_change.current,
                        new=onchain_hash,
                    )
                )
            await self._repair_snapshot(
                snapshot, snapshot_changes, onchain_hash, repaired, unrepaired
            )
            restored_copy_id = document.signed_copy_id
            if file_bad:
                assert current is not None and original is not None
                if tampered is not None and previous_hash is not None:
                    preserved = await self._store(
                        document, current, tampered, previous_hash, uploaded
                    )
                restored = await self._store(
                    document,
                    current,
                    original,
                    onchain_hash,
                    uploaded,
                    uploaded_by=user.user_id,
                    reason=(
                        RESTORED_REASON
                        if previous_hash is not None
                        else RESTORED_MISSING_REASON
                    ),
                )
                document.signed_copy_id = restored_copy_id = restored.id
                change.from_hash = previous_hash
                change.to_hash = onchain_hash
            await self._repair_metadata(document, metadata, repaired)
            reembed = await self._repair_analysis(document, analysis, repaired)
            change.changes = [r.model_dump() for r in repaired] or None
            await self.repo.add(change)
            await self.session.refresh(change, ["created_at"])
            await self.session.commit()
        except Exception as e:
            await self.session.rollback()
            for url in uploaded:
                if not await get_storage_client().delete_path(object_path_from_url(url)):
                    log.error(
                        "document.restore.cleanup_failed",
                        document_id=str(document_id),
                        url=url,
                    )
            if isinstance(e, IntegrityError):
                # The genuine hash is on another document of this lawyer's.
                log.error(
                    "document.restore.refused",
                    document_id=str(document_id),
                    error=str(e.orig),
                )
                raise ConflictError(
                    "This document can't be restored while another document holds "
                    "its hash. Restore that document first.",
                    details={"reason": "RESTORE_CONFLICT"},
                ) from e
            if isinstance(e, _STORAGE_ERRORS):
                log.error(
                    "document.restore.store_failed",
                    document_id=str(document_id),
                    error=f"{type(e).__name__}: {e}",
                )
                raise ServiceUnavailableError(
                    "The restored copy could not be stored. Try restoring again."
                ) from e
            raise

        if reembed:
            self._queue_reembed(document_id)
        log.info(
            "document.restore",
            user_id=str(user.user_id),
            document_id=str(document_id),
            previous_hash=previous_hash,
            restored_hash=onchain_hash,
            repaired=[f"{r.record}.{r.field}" for r in repaired],
            unrepaired=unrepaired,
        )
        assert restored_copy_id is not None
        return RestoreResponse(
            document_id=document_id,
            restored=True,
            previous_hash=previous_hash,
            restored_hash=onchain_hash,
            restored_copy_id=restored_copy_id,
            preserved_copy_id=preserved.id if preserved else None,
            restored_by=user.user_id,
            restored_at=change.created_at,
            repaired=repaired,
            unrepaired=unrepaired,
            verification=await self.verifier.verify_document(
                user, document_id, tamper_report=False
            ),
        )

    async def _restore_source(
        self, document_id: uuid.UUID, onchain_hash: str, *, required: bool
    ) -> bytes | None:
        """The archived PDF. When only its size would be judged by it, a lost
        archive isn't a reason to refuse the rest of the restore."""
        try:
            return await self._archived_original(document_id, onchain_hash)
        except (ConflictError, ServiceUnavailableError):
            if required:
                raise
            return None

    @staticmethod
    def _refuse_record(check: MetadataCheck, record: str = "record") -> None:
        """An unproven record is never written, and it can't be told to be fine."""
        if check.problem == "archive_mismatch":
            raise ConflictError(
                f"The archived {record} does not match the hash anchored on-chain.",
                details={"reason": "BACKUP_MISMATCH"},
            )
        if check.problem == "archive_missing":
            raise ConflictError(
                f"The archived {record} is missing, so this document cannot be restored.",
                details={"reason": "NO_BACKUP"},
            )
        raise ServiceUnavailableError(
            f"The chain or the archived {record} could not be read. Try restoring again."
        )

    async def _repair_analysis(
        self,
        document: Document,
        analysis: MetadataCheck,
        repaired: list[RestoredValue],
    ) -> bool:
        """Write the archived approved text, chunks and insight back. Returns
        whether any chunk was rewritten and so needs a new embedding."""
        if not analysis.changes:
            return False
        anchored = analysis.anchored
        assert anchored is not None  # ALTERED is only judged against the archive
        rows = await analysis_rows(self.session, document.id)
        extraction = rows.extraction
        approved = anchored["approved_text"]
        if extraction is None and (approved is not None or anchored["chunks"]):
            # Chunks hang off an extraction. The raw OCR isn't archived.
            extraction = DocumentExtraction(
                document_id=document.id,
                engine="restored",
                text="",
                page_count=0,
                meta={"restored_from": "analysis archive"},
            )
            self.session.add(extraction)
            await self.session.flush()
        if extraction is not None:
            if extraction.approved_text != approved:
                # The raw OCR is never rewritten: it is what the machine read.
                unreviewed = anchored["reviewed_at"] is None and extraction.text == approved
                extraction.reviewed_text = None if unreviewed else approved
            reviewer = anchored["reviewed_by"]
            extraction.reviewed_by = (
                uuid.UUID(reviewer)
                if reviewer and await self.session.get(User, uuid.UUID(reviewer))
                else None
            )
            extraction.reviewed_at = (
                datetime.fromisoformat(anchored["reviewed_at"])
                if anchored["reviewed_at"]
                else None
            )

        reembed = False
        live_chunks = {c.chunk_index: c for c in rows.chunks}
        for frozen in anchored["chunks"]:
            chunk = live_chunks.pop(frozen["chunk_index"], None)
            if chunk is not None and all(
                getattr(chunk, f) == frozen[f] for f in CHUNK_FIELDS
            ):
                continue
            if chunk is None:
                assert extraction is not None
                chunk = DocumentChunk(
                    document_id=document.id,
                    extraction_id=extraction.id,
                    chunk_index=frozen["chunk_index"],
                )
                self.session.add(chunk)
            for f in CHUNK_FIELDS:
                setattr(chunk, f, frozen[f])
            chunk.char_count = len(frozen["text"])
            chunk.embedding = None
            chunk.embedding_vec = None
            reembed = True
        for extra in live_chunks.values():
            await self.session.delete(extra)

        frozen_insight = anchored["insight"]
        insight = rows.insight
        if frozen_insight is None:
            if insight is not None:
                await self.session.delete(insight)
        else:
            if insight is None:
                insight = DocumentInsight(
                    document_id=document.id,
                    extraction_id=extraction.id if extraction else None,
                )
                self.session.add(insight)
            for f in INSIGHT_FIELDS:
                setattr(insight, f, frozen_insight[f])

        repaired.extend(
            RestoredValue(
                record="analysis", field=c.field, old=c.current, new=c.anchored
            )
            for c in analysis.changes
        )
        return reembed

    @staticmethod
    def _queue_reembed(document_id: uuid.UUID) -> None:
        """A broker outage leaves the restore done: the chunks wait for the next run."""
        from app.celery_tasks.documents import reembed_chunks_task

        try:
            reembed_chunks_task.delay()
        except Exception as e:
            log.error(
                "document.restore.reembed_not_queued",
                document_id=str(document_id),
                error=f"{type(e).__name__}: {e}",
            )

    async def _repair_snapshot(
        self,
        snapshot: DocumentSnapshot,
        changes: list[RecordChange],
        onchain_hash: str,
        repaired: list[RestoredValue],
        unrepaired: list[str],
    ) -> None:
        for change in changes:
            if change.field == "signed_copy_id":
                copy = await self._genuine_copy(snapshot.document_id, onchain_hash)
                if copy is None:
                    unrepaired.append("snapshot.signed_copy_id")
                    continue
                new: str | int | None = str(copy.id)
                snapshot.signed_copy_id = copy.id
            else:
                new = change.anchored
                setattr(snapshot, change.field, new)
            repaired.append(
                RestoredValue(
                    record="snapshot", field=change.field, old=change.current, new=new
                )
            )

    async def _genuine_copy(
        self, document_id: uuid.UUID, onchain_hash: str
    ) -> SignedCopy | None:
        """The first copy whose stored bytes hash to the chain. Its `sha256`
        column only narrows the search: a row can be edited to claim the hash."""
        for copy in await self.repo.signed_copies_with_hash(document_id, onchain_hash):
            try:
                content = await get_storage_client().download(copy.storage_url)
            except _STORAGE_ERRORS:
                continue
            if hashlib.sha256(content).hexdigest() == onchain_hash:
                return copy
        return None

    async def _repair_metadata(
        self,
        document: Document,
        metadata: MetadataCheck,
        repaired: list[RestoredValue],
    ) -> None:
        """Values from the archived JSON, which `RecordCheck` proved against the anchor."""
        for change in metadata.changes:
            value: Any = change.anchored
            if value is not None and change.field in _UUID_FIELDS:
                value = uuid.UUID(str(value))
                # The anchored user may be gone since, and the column can't name them.
                if change.field == "finalized_by" and not await self.session.get(User, value):
                    value = None
            elif value is not None and change.field == "finalized_at":
                value = datetime.fromisoformat(str(value))
            setattr(document, change.field, value)
            repaired.append(
                RestoredValue(
                    record="metadata",
                    field=change.field,
                    old=change.current,
                    new=None if value is None else change.anchored,
                )
            )

    async def _archived_original(
        self, document_id: uuid.UUID, onchain_hash: str
    ) -> bytes:
        """The archive's bytes, only if they are exactly what was anchored."""
        if self.backup is None:
            raise ConflictError(
                "There is no archived original to restore this document from.",
                details={"reason": "NO_BACKUP"},
            )
        try:
            key = archive_key(document_id, onchain_hash)
            # Asked first so a lost archive reads as gone for good, not as an outage.
            if not await self.backup.exists(key):
                log.error("document.restore.archive_missing", document_id=str(document_id))
                raise ConflictError(
                    "The archived original is missing, so this document cannot be "
                    "restored.",
                    details={"reason": "NO_BACKUP"},
                )
            data = await self.backup.fetch(key)
        except ConflictError:
            raise
        except Exception as e:
            log.error(
                "document.restore.archive_fetch_failed",
                document_id=str(document_id),
                error=f"{type(e).__name__}: {e}",
            )
            raise ServiceUnavailableError(
                "The archived original could not be read. Try restoring again."
            ) from e
        if hashlib.sha256(data).hexdigest() != onchain_hash:
            log.error("document.restore.archive_mismatch", document_id=str(document_id))
            raise ConflictError(
                "The archived original does not match the hash anchored on-chain.",
                details={"reason": "BACKUP_MISMATCH"},
            )
        return data

    async def _current_bytes(self, document: Document, judged_hash: str) -> bytes | None:
        """The file that was judged TAMPERED, to keep. None if it can no longer
        be read as that same file — the restore goes ahead without it."""
        copy = document.signed_copy
        assert copy is not None
        try:
            data = await get_storage_client().download(copy.storage_url)
        except _STORAGE_ERRORS as e:
            log.warning(
                "document.restore.tampered_unreadable",
                document_id=str(document.id),
                error=f"{type(e).__name__}: {e}",
            )
            return None
        if hashlib.sha256(data).hexdigest() != judged_hash:
            log.warning("document.restore.tampered_changed", document_id=str(document.id))
            return None
        return data

    async def _store(
        self,
        document: Document,
        current: SignedCopy,
        content: bytes,
        sha256: str,
        uploaded: list[str],
        uploaded_by: uuid.UUID | None = None,
        reason: str | None = None,
    ) -> SignedCopy:
        """Write `content` as a new signed copy at its own path. Never overwrites."""
        copy_id = uuid.uuid4()
        path = f"{document.user_id}/documents/{document.id}/{copy_id}.pdf"
        url = await get_storage_client().upload_new(
            file_path=path, file_bytes=content, content_type=current.content_type
        )
        uploaded.append(url)
        copy = SignedCopy(
            id=copy_id,
            document_id=document.id,
            storage_url=url,
            sha256=sha256,
            content_type=current.content_type,
            size_bytes=len(content),
            original_filename=current.original_filename,
            uploaded_by=uploaded_by,
            replaced_reason=reason,
        )
        await self.repo.add(copy)
        return copy
