"""Archive copies of finalized documents, and checks that they are still good.

The anchored hash is the reference: an archive is good only if its bytes hash
to the snapshot's `file_sha256`. Archiving at finalize is best-effort — a
storage outage must not undo a finalize that is already on-chain — so a gap is
left NULL and `verify` finds it.
"""

import hashlib
from datetime import datetime, timezone

from app.features.documents.models import SignedCopy
from app.infrastructure.backup import BackupProvider
from app.infrastructure.clients.supabase_storage import get_storage_client
from app.infrastructure.observability.logging_setup import log
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import DocumentSnapshot
from .schemas import BackupCheck, BackupVerificationResponse


def backup_key(snapshot: DocumentSnapshot) -> str:
    return f"{snapshot.document_id}/{snapshot.file_sha256}.pdf"


class SnapshotArchive:
    def __init__(self, session: AsyncSession, backup: BackupProvider) -> None:
        self.session = session
        self.backup = backup

    async def archive(self, snapshot: DocumentSnapshot, content: bytes) -> bool:
        """Store `content` as the snapshot's archive. The caller commits."""
        key = backup_key(snapshot)
        try:
            await self.backup.store(key, content, "application/pdf")
        except Exception as e:
            log.error(
                "backup.archive.failed",
                document_id=str(snapshot.document_id),
                key=key,
                error=str(e),
            )
            return False
        snapshot.backup_key = key
        snapshot.backed_up_at = datetime.now(timezone.utc)
        return True

    async def verify(self, repair: bool = False) -> BackupVerificationResponse:
        """Hash every archive against its anchored hash.

        With `repair`, a missing or bad archive is rewritten from the live
        signed copy — only when the live copy still matches the anchored hash.
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
        if hashlib.sha256(data).hexdigest() != snapshot.file_sha256:
            log.error(
                "backup.verify.mismatch",
                document_id=str(snapshot.document_id),
                key=snapshot.backup_key,
            )
            return "mismatch"
        return "ok"

    async def _repair(self, snapshot: DocumentSnapshot) -> str:
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
        if hashlib.sha256(content).hexdigest() != snapshot.file_sha256:
            log.error(
                "backup.repair.live_copy_changed", document_id=str(snapshot.document_id)
            )
            return "unrecoverable"
        if not await self.archive(snapshot, content):
            return "unrecoverable"
        log.info("backup.repaired", document_id=str(snapshot.document_id))
        return "repaired"
