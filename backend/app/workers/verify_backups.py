"""Scheduled check that every finalized document's archive is still good.

Report only — repair is a lawyer's call, through POST /admin/backups/verify.
Findings land in the logs as `backup.verify.*`.
"""

from app.features.snapshot.backups import SnapshotArchive
from app.infrastructure.backup import build_backup
from app.infrastructure.database import AsyncSessionLocal


async def verify_backups() -> None:
    async with AsyncSessionLocal() as session:
        await SnapshotArchive(session, build_backup()).verify()
