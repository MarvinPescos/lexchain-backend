"""Restore: a TAMPERED finalized document gets its anchored bytes back.

The bytes come only from the archive written at finalize, and only once they
re-hash to the on-chain hash — never from the active file, which is the one
that no longer matches. The file found in its place is kept as its own signed
copy. The document stays FINALIZED and nothing is re-anchored: the original
chain record is what the restored copy verifies against.
"""

import hashlib
import uuid

import httpx
from storage3.utils import StorageException

from app.features.documents.schemas import RestoreResponse
from app.features.snapshot.backups import matches_snapshot
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.infrastructure.backup import BackupProvider
from app.infrastructure.blockchain import BlockchainProvider
from app.infrastructure.clients.supabase_storage import (
    get_storage_client,
    object_path_from_url,
)
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.errors.exceptions import ConflictError, ServiceUnavailableError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import lifecycle
from ..access import DocumentAccess
from ..constants import ACTION_RESTORE
from ..models import Document, SignedCopy
from ..repository import DocumentRepository

RESTORED_REASON = (
    "Restored from the archived original: the stored file no longer matched "
    "the hash anchored on-chain."
)

_STORAGE_ERRORS = (httpx.HTTPError, OSError, StorageException)


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
        self.snapshot_repo = DocumentSnaphotRepository(session)
        # None when BACKUP_ENABLED is off: there is no archive to restore from.
        self.backup = backup
        from app.features.blockchain.service import BlockchainService

        self.verifier = BlockchainService(session, provider)

    async def restore(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> RestoreResponse:
        """Make the archived, anchored bytes the current signed copy again.

        Only from TAMPERED with a trusted baseline. Every refusal happens before
        anything is written. Already AUTHENTIC answers `restored=False`.
        """
        document = await self.access.authorize(user, document_id, ACTION_RESTORE)
        # One restore at a time: a concurrent retry waits, then finds it authentic.
        await self.session.refresh(document, with_for_update=True)

        verdict = await self.verifier.verify_document(
            user, document_id, tamper_report=False
        )
        if verdict.status == "AUTHENTIC":
            assert verdict.current_hash is not None
            assert document.signed_copy_id is not None
            return RestoreResponse(
                document_id=document_id,
                restored=False,
                restored_hash=verdict.current_hash,
                restored_copy_id=document.signed_copy_id,
                verification=verdict,
            )
        if verdict.status == "SNAPSHOT_COMPROMISED":
            raise ConflictError(
                "The frozen record was altered as well, so there is no trusted "
                "original to restore from.",
                details={"reason": "SNAPSHOT_COMPROMISED"},
            )
        if verdict.status != "TAMPERED":
            raise ServiceUnavailableError(
                "Could not read the stored file to confirm it was tampered with. "
                "Try again once storage is available."
            )

        onchain_hash = verdict.onchain_hash
        assert onchain_hash is not None and verdict.current_hash is not None
        original = await self._archived_original(document_id, onchain_hash)
        tampered = await self._current_bytes(document, verdict.current_hash)

        current = document.signed_copy
        assert current is not None
        uploaded: list[str] = []
        try:
            preserved = None
            if tampered is not None:
                preserved = await self._store(
                    document, current, tampered, verdict.current_hash, uploaded
                )
            restored = await self._store(
                document,
                current,
                original,
                onchain_hash,
                uploaded,
                uploaded_by=user.user_id,
                reason=RESTORED_REASON,
            )
            document.signed_copy_id = restored.id
            document.document_hash = onchain_hash
            change = lifecycle.transition(ACTION_RESTORE, document, user.user_id)
            change.from_hash = verdict.current_hash
            change.to_hash = onchain_hash
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

        log.info(
            "document.restore",
            user_id=str(user.user_id),
            document_id=str(document_id),
            previous_hash=verdict.current_hash,
            restored_hash=onchain_hash,
        )
        return RestoreResponse(
            document_id=document_id,
            restored=True,
            previous_hash=verdict.current_hash,
            restored_hash=onchain_hash,
            restored_copy_id=restored.id,
            preserved_copy_id=preserved.id if preserved else None,
            restored_by=user.user_id,
            restored_at=change.created_at,
            verification=await self.verifier.verify_document(
                user, document_id, tamper_report=False
            ),
        )

    async def _archived_original(
        self, document_id: uuid.UUID, onchain_hash: str
    ) -> bytes:
        """The archive's bytes, only if they are exactly what was anchored."""
        snapshot = await self.snapshot_repo.get_by_document_id(document_id)
        if self.backup is None or snapshot is None or snapshot.backup_key is None:
            raise ConflictError(
                "There is no archived original to restore this document from.",
                details={"reason": "NO_BACKUP"},
            )
        try:
            # Asked first so a lost archive reads as gone for good, not as an outage.
            if not await self.backup.exists(snapshot.backup_key):
                log.error("document.restore.archive_missing", document_id=str(document_id))
                raise ConflictError(
                    "The archived original is missing, so this document cannot be "
                    "restored.",
                    details={"reason": "NO_BACKUP"},
                )
            data = await self.backup.fetch(snapshot.backup_key)
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
        # The snapshot hash is the on-chain hash here: the verdict was TAMPERED,
        # which is only given against a trusted baseline.
        if snapshot.file_sha256 != onchain_hash or not matches_snapshot(snapshot, data):
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
