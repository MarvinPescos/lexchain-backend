"""Recover: put a deleted document back from the copy its delete left behind.

Nothing here proves the copy genuine, as the chain does for a finalized document:
it is what the row held when it was deleted. It stays a person's click.
"""

import json
import uuid
from datetime import datetime, timezone

from app.infrastructure.clients.supabase_storage import get_storage_client
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.errors.exceptions import ConflictError, NotFoundError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import ACTION_RECOVER
from ..models import DeletedDocument, DocumentStageChange
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
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
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

        now = datetime.now(timezone.utc)
        try:
            async with self.session.begin_nested():
                await self._insert(deleted)
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
