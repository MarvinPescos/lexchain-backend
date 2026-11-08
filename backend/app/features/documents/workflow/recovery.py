"""Recover: put a deleted document back from the copy its delete left behind.

Nothing here proves the copy genuine, as the chain does for a finalized document:
it is what the row held when it was deleted. It stays a person's click.

The snapshot is the exception. Its RESTRICT means it is deleted before the
document, so the trigger never saves it. For a document anchored on-chain it is
rebuilt from the anchored hash instead; anything else that no longer matches is
then a tamper finding that Restore repairs.
"""

import json
import uuid
from datetime import datetime, timezone

from app.features.snapshot.models import DocumentSnapshot
from app.features.snapshot.records import archive_key, normalise_hash
from app.infrastructure.blockchain import BlockchainProvider
from app.infrastructure.clients.supabase_storage import get_storage_client
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.errors.exceptions import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import ACTION_RECOVER, LIFECYCLE_FINALIZED
from ..models import DeletedDocument, DocumentStageChange, SignedCopy
from ..repository import DeletedDocumentRepository
from ..schemas import RecoverResponse

# Parents first. Must match the tables `capture_deleted_document` saves.
RECOVERED_TABLES = (
    "signed_copies",
    "document_stage_changes",
    "document_parties",
    "draft_comments",
    "draft_replies",
    "document_extraction",
    "document_chunks",
    "document_insight",
    "draft_revocations",
    "processing_dispatches",
    "ecopy_requests",
    "ecopy_events",
    "on_chain_records",
    "verification_results",
)


class DocumentRecovery:
    def __init__(self, session: AsyncSession, chain: BlockchainProvider) -> None:
        self.session = session
        self.chain = chain
        self.repo = DeletedDocumentRepository(session)

    async def recover(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> RecoverResponse:
        deleted = await self.repo.get_open(document_id, user.user_id)
        if deleted is None:
            raise NotFoundError("No deleted document with this ID.")
        if deleted.document is None or deleted.related is None:
            raise ConflictError(
                "No copy of this document was saved. It needs a database backup.",
                details={"reason": "BACKUP_NEEDED"},
            )

        # Read before anything is written: a document back without its
        # snapshot can't be verified at all.
        onchain_hash = await self._anchored_hash(deleted)
        now = datetime.now(timezone.utc)
        try:
            async with self.session.begin_nested():
                await self._insert(deleted)
                if onchain_hash is not None:
                    await self._rebuild_snapshot(deleted, onchain_hash)
                self.session.add(
                    DocumentStageChange(
                        document_id=document_id,
                        to_stage=deleted.document["lifecycle"],
                        action=ACTION_RECOVER,
                        actor_id=user.user_id,
                    )
                )
                deleted.recovered_at = now
        except IntegrityError as e:
            # Its book or a user it points at is gone, or its place was taken.
            log.error(
                "document.recover.refused",
                document_id=str(document_id),
                error=str(e.orig),
            )
            raise ConflictError(
                "This document can't go back: something it depends on has changed "
                "since it was deleted. It needs a database backup.",
                details={"reason": "RECOVERY_CONFLICT"},
            ) from e
        await self.session.commit()

        log.info(
            "document.recover", user_id=str(user.user_id), document_id=str(document_id)
        )
        return RecoverResponse(
            document_id=document_id,
            file_name=deleted.file_name,
            file_present=await self._file_present(deleted),
            recovered_at=now,
        )

    async def _insert(self, deleted: DeletedDocument) -> None:
        assert deleted.document is not None and deleted.related is not None
        # Null first: the current copy and the document point at each other.
        await self._load(
            "documents", [{**deleted.document, "signed_copy_id": None}]
        )
        for table in RECOVERED_TABLES:
            await self._load(table, deleted.related.get(table, []))
        await self.session.execute(
            text("UPDATE documents SET signed_copy_id = :copy WHERE id = :id"),
            {
                "copy": deleted.document["signed_copy_id"],
                "id": deleted.document_id,
            },
        )

    async def _anchored_hash(self, deleted: DeletedDocument) -> str | None:
        assert deleted.document is not None
        try:
            record = await self.chain.get_document_hash(
                document_id=str(deleted.document_id),
                retry_empty=deleted.document["lifecycle"] == LIFECYCLE_FINALIZED,
            )
        except Exception as e:
            log.error(
                "document.recover.chain_unavailable",
                document_id=str(deleted.document_id),
                error=f"{type(e).__name__}: {e}",
            )
            raise ServiceUnavailableError(
                "Could not read the chain to rebuild this document's frozen record. "
                "Try recovering again once it is reachable."
            ) from e
        return normalise_hash(record.data_hash) if record is not None else None

    async def _rebuild_snapshot(
        self, deleted: DeletedDocument, onchain_hash: str
    ) -> None:
        """The snapshot as finalize wrote it, from the anchored hash. The copy is
        the one claiming that hash, the current one first; failing that, the
        current copy, which verify then reports and Restore replaces."""
        assert deleted.document is not None
        document_id = deleted.document_id
        current = deleted.document["signed_copy_id"]
        copies = await self.session.scalars(
            select(SignedCopy)
            .where(SignedCopy.document_id == document_id)
            .order_by(SignedCopy.created_at.desc())
        )
        candidates = sorted(
            copies.all(),
            key=lambda c: (c.sha256 != onchain_hash, str(c.id) != current),
        )
        if not candidates:
            log.error("document.recover.no_signed_copy", document_id=str(document_id))
            return
        copy = candidates[0]
        self.session.add(
            DocumentSnapshot(
                document_id=document_id,
                signed_copy_id=copy.id,
                file_sha256=onchain_hash,
                size_bytes=copy.size_bytes,
                backup_key=archive_key(document_id, onchain_hash),
            )
        )
        log.warning(
            "document.recover.snapshot_rebuilt",
            document_id=str(document_id),
            signed_copy_id=str(copy.id),
            genuine=copy.sha256 == onchain_hash,
        )

    async def _load(self, table: str, rows: list[dict]) -> None:
        if rows:
            await self.session.execute(
                text(
                    f"INSERT INTO {table} "
                    f"SELECT * FROM jsonb_populate_recordset(NULL::{table}, CAST(:rows AS jsonb))"
                ),
                {"rows": json.dumps(rows)},
            )

    async def _file_present(self, deleted: DeletedDocument) -> bool | None:
        assert deleted.document is not None and deleted.related is not None
        copy_id = deleted.document["signed_copy_id"]
        urls = [
            c["storage_url"]
            for c in deleted.related.get("signed_copies", [])
            if c["id"] == copy_id
        ]
        if not urls:
            return None
        try:
            await get_storage_client().download(urls[0])
        except FileNotFoundError:
            return False
        except Exception as e:
            log.error(
                "document.recover.file_unread",
                document_id=str(deleted.document_id),
                error=str(e),
            )
            return None
        return True
