"""Scheduled check for documents that have disappeared."""

from app.core import settings
from app.features.documents.workflow.deletion_watch import DeletionWatch
from app.infrastructure.backup import build_backup
from app.infrastructure.clients.supabase_storage import get_storage_client
from app.infrastructure.database import AsyncSessionLocal


async def watch_deletions() -> None:
    backup = build_backup() if settings.BACKUP_ENABLED else None
    async with AsyncSessionLocal() as session:
        await DeletionWatch(session, get_storage_client(), backup).run()
