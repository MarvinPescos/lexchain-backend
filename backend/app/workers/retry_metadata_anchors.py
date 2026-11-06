"""Scheduled retry of metadata anchors that failed at finalize.

Finds them from the chain, not from a database flag: a snapshot with a
`metadata_sha256` and no anchor is one that's still owed.
"""

from app.features.blockchain.dependencies import get_blockchain_provider
from app.features.snapshot.backups import SnapshotArchive
from app.infrastructure.backup import build_backup
from app.infrastructure.database import AsyncSessionLocal
from app.infrastructure.observability.logging_setup import log


async def retry_metadata_anchors() -> int:
    async with AsyncSessionLocal() as session:
        anchored = await SnapshotArchive(
            session, build_backup(), get_blockchain_provider()
        ).anchor_pending_metadata()
    log.info("worker.metadata_anchor.done", anchored=anchored)
    return anchored
