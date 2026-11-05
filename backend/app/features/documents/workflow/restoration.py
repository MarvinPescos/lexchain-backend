"""Restore: a TAMPERED or FILE_MISSING finalized document gets its anchored bytes back.

The bytes come only from the archive written at finalize, and only once they
re-hash to the on-chain hash — never from the active file, which is the one
that no longer matches. The archive is found from the on-chain hash, so an
altered snapshot row can neither block a restore nor choose what is restored.
The file found in its place, if any, is kept as its own signed copy. The document stays FINALIZED and nothing is re-anchored: the original
chain record is what the restored copy verifies against.
"""

import hashlib
import uuid

import httpx
from storage3.utils import StorageException

from app.features.documents.schemas import RestoreResponse
from app.features.snapshot.backups import archive_key
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
RESTORED_MISSING_REASON = "Restored from the archived original: the stored file was missing."

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
        # None when BACKUP_ENABLED is off: there is no archive to restore from.
        self.backup = backup
        from app.features.blockchain.service import BlockchainService

        self.verifier = BlockchainService(session, provider)

    async def restore(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> RestoreResponse:
        """Make the archived, anchored bytes the current signed copy again.

        Only from TAMPERED, FILE_MISSING or SNAPSHOT_COMPROMISED. Every refusal happens
        before anything is written. Already AUTHENTIC answers `restored=False`.
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
        if verdict.status not in ("TAMPERED", "FILE_MISSING", "SNAPSHOT_COMPROMISED"):
            raise ServiceUnavailableError(
                "Could not read the stored file or the chain to confirm it was "
                "tampered with. Try again once both are available."
            )

        current = document.signed_copy
        if current is None:
            raise ConflictError(
                "This document has no signed copy record to restore onto.",
                details={"reason": "NO_SIGNED_COPY"},
            )

        onchain_hash = verdict.onchain_hash
        previous_hash = verdict.current_hash  # None for a deleted file
        assert onchain_hash is not None
        original = await self._archived_original(document_id, onchain_hash)
        tampered = (
            await self._current_bytes(document, previous_hash)
            if previous_hash is not None
            else None
        )

        uploaded: list[str] = []
        try:
            preserved = None
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
                reason=RESTORED_REASON if previous_hash is not None else RESTORED_MISSING_REASON,
            )
            document.signed_copy_id = restored.id
            document.document_hash = onchain_hash
            change = lifecycle.transition(ACTION_RESTORE, document, user.user_id)
            change.from_hash = previous_hash
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
            previous_hash=previous_hash,
            restored_hash=onchain_hash,
        )
        return RestoreResponse(
            document_id=document_id,
            restored=True,
            previous_hash=previous_hash,
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
