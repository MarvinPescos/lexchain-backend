"""Archive copies of finalized documents, and checks that they are still good.

The anchored hash is the reference: an archive is good only if its bytes hash
to the snapshot's `file_sha256` and match its `size_bytes`. Finalize archives
before it anchors, and refuses to anchor when the archive cannot be read back
intact. Snapshots from before that guarantee have a NULL `backup_key`;
`verify(repair=True)` archives them only when the snapshot agrees with the chain
and the live copy still matches it. Archives are write-once: an existing object
is reused if it checks out and never overwritten if it doesn't.
"""

import hashlib
from datetime import datetime, timezone

from app.features.documents.models import SignedCopy
from app.infrastructure.backup import BackupProvider
from app.infrastructure.blockchain import BlockchainProvider
from app.infrastructure.clients.supabase_storage import get_storage_client
from app.infrastructure.observability.logging_setup import log
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import DocumentSnapshot
from .schemas import BackupCheck, BackupVerificationResponse


def backup_key(snapshot: DocumentSnapshot) -> str:
    return f"{snapshot.document_id}/{snapshot.file_sha256}.pdf"


def _normalise(value: str) -> str:
    """Chain reads come back with or without the 0x prefix depending on the provider."""
    return value.lower().removeprefix("0x")


def _matches(snapshot: DocumentSnapshot, data: bytes) -> bool:
    return (
        len(data) == snapshot.size_bytes
        and hashlib.sha256(data).hexdigest() == snapshot.file_sha256
    )


class SnapshotArchive:
    def __init__(
        self,
        session: AsyncSession,
        backup: BackupProvider,
        chain: BlockchainProvider | None = None,
    ) -> None:
        self.session = session
        self.backup = backup
        # Needed only by repair, which refuses to run without it.
        self.chain = chain

    async def archive(self, snapshot: DocumentSnapshot, content: bytes) -> bool:
        """Store `content` as the snapshot's archive, then read it back and check
        it against the snapshot. The key is recorded only once it checks out.
        The caller commits.
        """
        key = backup_key(snapshot)
        try:
            # A retry after a failed anchor finds the object already there.
            if not await self.backup.exists(key):
                await self.backup.store(key, content, "application/pdf")
            stored = await self.backup.fetch(key)
        except Exception as e:
            log.error(
                "backup.archive.failed",
                document_id=str(snapshot.document_id),
                key=key,
                error=str(e),
            )
            return False
        if not _matches(snapshot, stored):
            log.error(
                "backup.archive.readback_mismatch",
                document_id=str(snapshot.document_id),
                key=key,
            )
            return False
        snapshot.backup_key = key
        snapshot.backed_up_at = datetime.now(timezone.utc)
        return True

    async def verify(self, repair: bool = False) -> BackupVerificationResponse:
        """Hash every archive against its anchored hash.

        With `repair`, a missing archive is written from the live signed copy —
        only when the snapshot matches the chain and the live copy matches the
        snapshot. A bad archive is reported, never overwritten.
        """
        result = await self.session.execute(
            select(DocumentSnapshot).order_by(DocumentSnapshot.created_at)
        )
        snapshots = list(result.scalars().all())

        counts = {s: 0 for s in ("ok", "missing", "mismatch", "repaired", "unrecoverable")}
        problems: list[BackupCheck] = []
        for snapshot in snapshots:
            status = await self._check(snapshot)
            if status != "ok" and repair:
                status = await self._repair(snapshot)
            counts[status] += 1
            if status != "ok":
                problems.append(
                    BackupCheck(
                        document_id=snapshot.document_id,
                        status=status,
                        backup_key=snapshot.backup_key,
                    )
                )
        if repair:
            await self.session.commit()

        log_fn = log.warning if problems else log.info
        log_fn("backup.verify.done", checked=len(snapshots), repair=repair, **counts)
        return BackupVerificationResponse(
            checked=len(snapshots), problems=problems, **counts
        )

    async def _check(self, snapshot: DocumentSnapshot) -> str:
        if snapshot.backup_key is None:
            return "missing"
        try:
            data = await self.backup.fetch(snapshot.backup_key)
        except Exception as e:
            log.error(
                "backup.verify.fetch_failed",
                document_id=str(snapshot.document_id),
                key=snapshot.backup_key,
                error=str(e),
            )
            return "missing"
        if not _matches(snapshot, data):
            log.error(
                "backup.verify.mismatch",
                document_id=str(snapshot.document_id),
                key=snapshot.backup_key,
            )
            return "mismatch"
        return "ok"

    async def _repair(self, snapshot: DocumentSnapshot) -> str:
        if not await self._anchored(snapshot):
            return "unrecoverable"
        copy = await self.session.get(SignedCopy, snapshot.signed_copy_id)
        try:
            content = await get_storage_client().download(copy.storage_url)
        except Exception as e:
            log.error(
                "backup.repair.download_failed",
                document_id=str(snapshot.document_id),
                error=str(e),
            )
            return "unrecoverable"
        if not _matches(snapshot, content):
            log.error(
                "backup.repair.live_copy_changed", document_id=str(snapshot.document_id)
            )
            return "unrecoverable"
        if not await self.archive(snapshot, content):
            return "unrecoverable"
        log.info("backup.repaired", document_id=str(snapshot.document_id))
        return "repaired"

    async def _anchored(self, snapshot: DocumentSnapshot) -> bool:
        """Whether the snapshot's hash is the one on-chain. The snapshot row is
        only a database copy; nothing is archived as the original on its word.
        """
        document_id = str(snapshot.document_id)
        if self.chain is None:
            log.error("backup.repair.no_chain", document_id=document_id)
            return False
        try:
            record = await self.chain.get_document_hash(document_id=document_id)
        except Exception as e:
            log.error("backup.repair.chain_read_failed", document_id=document_id, error=str(e))
            return False
        if record is None or _normalise(record.data_hash) != snapshot.file_sha256:
            log.error("backup.repair.snapshot_not_anchored", document_id=document_id)
            return False
        return True
