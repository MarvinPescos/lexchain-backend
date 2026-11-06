"""Finds documents that have disappeared, in three layers.

The chain only knows finalized documents, so an unfinalized one has nothing
outside the database vouching for it. Each layer covers the one before it being
switched off by someone with database access:

1. The trigger on `documents` saves the row and its children at the moment of
   deletion, so these can be recovered.
2. A list of document IDs, written hourly to the backup store, catches a row that
   vanished without the trigger. A document made and deleted between two lists
   is missed.
3. A stored PDF with no document row catches that one too, unless the file went
   as well.

Layers 2 and 3 can only say that something is gone: the row needs a database
backup. A layer that can't be read is logged and skipped, so an outage neither
stops the others nor reads as a deletion.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

from app.core import settings
from app.features.notifications.models import NotificationType
from app.features.notifications.service import NotificationService
from app.features.users.models import User
from app.features.users.repository import UserRepository
from app.infrastructure.backup import BackupProvider
from app.infrastructure.backup.alerts import send_backup_alert
from app.infrastructure.clients.supabase_storage import SupabaseStorageClient
from app.infrastructure.observability.logging_setup import log
from app.shared.constants import ROLE_LAWYER
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import (
    DELETION_SOURCE_LEDGER,
    DELETION_SOURCE_STORAGE,
    DELETION_SOURCE_TRIGGER,
)
from ..models import DeletedDocument
from ..repository import DeletedDocumentRepository


def ledger_key(at: datetime) -> str:
    # A folder per day: Supabase's `exists` only sees a folder's first 100 files.
    return f"document-ledger/{at:%Y-%m-%d}/{at:%H}.json"


class DeletionWatch:
    def __init__(
        self,
        session: AsyncSession,
        storage: SupabaseStorageClient,
        backup: BackupProvider | None,
    ) -> None:
        self.session = session
        self.storage = storage
        self.backup = backup
        self.repo = DeletedDocumentRepository(session)
        self.notifications = NotificationService(session)
        self.user_repo = UserRepository(session)

    async def run(self) -> None:
        now = datetime.now(timezone.utc)
        await self.repo.close_recovered(now)
        ledger = await self._last_ledger(now)
        folders = await self._stored_folders()

        # Read after both, so a document made meanwhile finds its row.
        live = await self.repo.list_live()
        known = await self.repo.open_document_ids() | set(live)
        found: list[DeletedDocument] = []
        for entry in ledger:
            document_id = uuid.UUID(entry["id"])
            if document_id not in known:
                known.add(document_id)
                found.append(
                    DeletedDocument(
                        document_id=document_id,
                        user_id=uuid.UUID(entry["user_id"]),
                        file_name=entry["file_name"],
                        source=DELETION_SOURCE_LEDGER,
                    )
                )
        for owner, folder in folders:
            document_id = uuid.UUID(folder)
            if document_id not in known and await self._is_orphan(owner, folder, now):
                known.add(document_id)
                found.append(
                    DeletedDocument(
                        document_id=document_id,
                        user_id=uuid.UUID(owner),
                        source=DELETION_SOURCE_STORAGE,
                    )
                )
        self.session.add_all(found)
        await self.session.flush()

        alerted = await self._alert(now)
        await self._write_ledger(now, live)
        log.info("document.deletions.done", found=len(found), alerted=alerted)

    async def _last_ledger(self, now: datetime) -> list[dict]:
        """The newest list of document IDs, or none on a first run or a failure."""
        if self.backup is None:
            return []
        try:
            for hours in range(settings.DELETION_LEDGER_LOOKBACK_HOURS + 1):
                key = ledger_key(now - timedelta(hours=hours))
                if await self.backup.exists(key):
                    return json.loads(await self.backup.fetch(key))["documents"]
        except Exception as e:
            log.error("document.deletions.ledger_unreadable", error=str(e))
            return []
        log.warning("document.deletions.no_ledger")
        return []

    async def _stored_folders(self) -> list[tuple[str, str]]:
        try:
            return await self.storage.list_document_folders()
        except Exception as e:
            log.error("document.deletions.storage_unreadable", error=str(e))
            return []

    async def _is_orphan(self, owner: str, folder: str, now: datetime) -> bool:
        try:
            uploaded = await self.storage.latest_upload(owner, folder)
        except Exception as e:
            log.error("document.deletions.storage_unreadable", error=str(e))
            return False
        if uploaded is None:
            return False
        since = settings.DELETION_ORPHANS_SINCE
        if since is not None and uploaded < since:
            return False
        grace = timedelta(seconds=settings.DELETION_ORPHAN_GRACE_SECONDS)
        return now - uploaded >= grace

    async def _alert(self, now: datetime) -> int:
        """Tell the owner of each new finding, or every lawyer if they're gone."""
        unalerted = await self.repo.list_unalerted()
        emails: list[tuple[str, str]] = []
        for deleted in unalerted:
            title, body = self._message(deleted)
            for user_id in await self._recipients(deleted):
                await self.notifications.create(
                    user_id=user_id,
                    type=NotificationType.DOCUMENT_DELETED,
                    title=title,
                    body=body,
                    event_metadata={
                        "document_id": str(deleted.document_id),
                        "source": deleted.source,
                        "cascaded": deleted.cascaded,
                    },
                )
            deleted.alerted_at = now
            emails.append((title, body))
            log.error(
                "document.deletions.found",
                document_id=str(deleted.document_id),
                source=deleted.source,
            )
        await self.session.commit()
        for subject, body in emails:
            try:
                await send_backup_alert(subject, body)
            except Exception as e:
                log.error("backup.alert.email_failed", error=str(e))
        return len(unalerted)

    async def _recipients(self, deleted: DeletedDocument) -> list[uuid.UUID]:
        if deleted.user_id and await self.session.get(User, deleted.user_id):
            return [deleted.user_id]
        return await self.user_repo.list_ids_by_role(ROLE_LAWYER)

    @staticmethod
    def _message(deleted: DeletedDocument) -> tuple[str, str]:
        name = deleted.file_name or f"Document {deleted.document_id}"
        if deleted.source == DELETION_SOURCE_TRIGGER:
            return (
                "Document deleted",
                (
                    f"{name} was deleted from the database. A copy was saved, so "
                    "you can bring it back from Tamper Reports."
                ),
            )
        return (
            "Document deleted without a trace",
            (
                f"{name} is gone from the database and no copy was saved, so the "
                "deletion may have been hidden. Bring it back from a database backup."
            ),
        )

    async def _write_ledger(
        self, now: datetime, live: dict[uuid.UUID, tuple[uuid.UUID, str | None]]
    ) -> None:
        if self.backup is None:
            return
        key = ledger_key(now)
        documents = [
            {"id": str(document_id), "user_id": str(user_id), "file_name": file_name}
            for document_id, (user_id, file_name) in live.items()
        ]
        try:
            # Write-once: a second run in the same hour keeps the first list.
            if not await self.backup.exists(key):
                await self.backup.store(
                    key,
                    json.dumps({"documents": documents}).encode(),
                    "application/json",
                )
        except Exception as e:
            log.error("document.deletions.ledger_not_written", error=str(e))
