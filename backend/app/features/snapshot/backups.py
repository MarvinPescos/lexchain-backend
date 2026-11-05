"""Archive copies of finalized documents, and checks that they are still good.

The anchored hash is the reference: an archive is good only if its bytes hash
to the snapshot's `file_sha256` and match its `size_bytes`. Finalize archives
before it anchors, and refuses to anchor when the archive cannot be read back
intact. Snapshots from before that guarantee have a NULL `backup_key`;
`verify(repair=True)` archives them only when the snapshot agrees with the chain
and the live copy still matches it. Archives are write-once: an existing object
is reused if it checks out and never overwritten if it doesn't.

`verify` also checks each document's live signed copy against the chain, so a
document nobody opens is still covered. A mismatch is recorded and alerted once
per tampered file; a read failure is unknown and neither.
"""

import hashlib
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import get_args

from app.features.blockchain.constants import (
    VERIFY_AUTHENTIC,
    VERIFY_FILE_MISSING,
    VERIFY_SNAPSHOT_COMPROMISED,
    VERIFY_TAMPER_FINDINGS,
    VERIFY_TAMPERED,
    VERIFY_UNAVAILABLE,
)
from app.features.blockchain.models import VerificationResult
from app.features.blockchain.repository import (
    OnChainRecordRepository,
    VerificationResultRepository,
)
from app.features.documents.models import Document, SignedCopy
from app.features.notifications.models import NotificationType
from app.features.notifications.service import NotificationService
from app.infrastructure.backup import BackupProvider
from app.infrastructure.blockchain import BlockchainProvider
from app.infrastructure.clients.supabase_storage import get_storage_client
from app.infrastructure.observability.logging_setup import log
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import DocumentSnapshot
from .schemas import (
    BackupCheck,
    BackupStatus,
    BackupVerificationResponse,
    LiveFileCheck,
    LiveFileReport,
)

BACKUP_STATUSES = get_args(BackupStatus)

_LIVE_ALERTS: dict[str, tuple[str, str]] = {
    VERIFY_TAMPERED: (
        "Document file altered",
        "The stored file of {file_name} no longer matches the hash anchored on-chain.",
    ),
    VERIFY_FILE_MISSING: (
        "Document file missing",
        "The stored file of {file_name} was deleted.",
    ),
    VERIFY_SNAPSHOT_COMPROMISED: (
        "Document file and record altered",
        (
            "The stored file of {file_name} no longer matches the hash anchored "
            "on-chain, and its frozen record in the database was altered as well."
        ),
    ),
}


def archive_key(document_id: uuid.UUID, file_hash: str) -> str:
    return f"{document_id}/{file_hash}.pdf"


def backup_key(snapshot: DocumentSnapshot) -> str:
    return archive_key(snapshot.document_id, snapshot.file_sha256)


def _normalise(value: str) -> str:
    """Chain reads come back with or without the 0x prefix depending on the provider."""
    return value.lower().removeprefix("0x")


def matches_snapshot(snapshot: DocumentSnapshot, data: bytes) -> bool:
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
        # Needed by verify and repair. Without it nothing can be called good.
        self.chain = chain
        self.notifications = NotificationService(session)
        self.onchain_repo = OnChainRecordRepository(session)
        self.verification_repo = VerificationResultRepository(session)

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
        if not matches_snapshot(snapshot, stored):
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
        """Hash every archive against the snapshot and the hash read from the chain.

        An archive the chain could not confirm is `unverified`: integrity unknown,
        never repaired. With `repair`, a missing archive is written from the live signed copy —
        only when the snapshot matches the chain and the live copy matches the
        snapshot. A bad archive is reported, never overwritten.

        Every live file is checked too, whatever its archive's status.
        """
        result = await self.session.execute(
            select(DocumentSnapshot).order_by(DocumentSnapshot.created_at)
        )
        snapshots = list(result.scalars().all())

        counts = {s: 0 for s in BACKUP_STATUSES}
        problems: list[BackupCheck] = []
        live_counts: Counter[str] = Counter()
        live_problems: list[LiveFileCheck] = []
        for snapshot in snapshots:
            onchain_hash = await self._onchain_hash(snapshot)
            status = await self._check(snapshot, onchain_hash)
            if status in ("missing", "mismatch") and repair:
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
            live_status = await self._check_live(snapshot, onchain_hash)
            live_counts[live_status] += 1
            if live_status != VERIFY_AUTHENTIC:
                live_problems.append(
                    LiveFileCheck(document_id=snapshot.document_id, status=live_status)
                )
        if repair or any(live_counts[s] for s in VERIFY_TAMPER_FINDINGS):
            await self.session.commit()

        live = LiveFileReport(
            checked=len(snapshots),
            ok=live_counts[VERIFY_AUTHENTIC],
            tampered=live_counts[VERIFY_TAMPERED],
            file_missing=live_counts[VERIFY_FILE_MISSING],
            snapshot_compromised=live_counts[VERIFY_SNAPSHOT_COMPROMISED],
            unavailable=live_counts[VERIFY_UNAVAILABLE],
            problems=live_problems,
        )
        log_fn = log.warning if problems or live_problems else log.info
        log_fn(
            "backup.verify.done",
            checked=len(snapshots),
            repair=repair,
            live=dict(live_counts),
            **counts,
        )
        return BackupVerificationResponse(
            checked=len(snapshots), problems=problems, live=live, **counts
        )

    async def _check(
        self, snapshot: DocumentSnapshot, onchain_hash: str | None
    ) -> BackupStatus:
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
        if not matches_snapshot(snapshot, data):
            log.error(
                "backup.verify.mismatch",
                document_id=str(snapshot.document_id),
                key=snapshot.backup_key,
            )
            return "mismatch"
        if onchain_hash is None:
            return "unverified"
        if onchain_hash != snapshot.file_sha256:
            log.error(
                "backup.verify.chain_mismatch",
                document_id=str(snapshot.document_id),
                key=snapshot.backup_key,
            )
            return "mismatch"
        return "ok"

    async def _check_live(
        self, snapshot: DocumentSnapshot, onchain_hash: str | None
    ) -> str:
        """Hash the document's current signed copy against the chain, and alert
        on a mismatch. Returns the verdict.

        Not `BlockchainService._verify`: it needs an authenticated user. The
        snapshot's own `signed_copy_id` is the original, not the restored copy.
        """
        document_id = str(snapshot.document_id)
        if onchain_hash is None:
            return VERIFY_UNAVAILABLE

        document = await self.session.get(Document, snapshot.document_id)
        assert document is not None  # the snapshot's foreign key
        copy = (
            await self.session.get(SignedCopy, document.signed_copy_id)
            if document.signed_copy_id
            else None
        )
        current_hash: str | None = None
        if copy is not None:
            try:
                # Never through the CDN: see SupabaseStorageClient.download.
                content = await get_storage_client().download(copy.storage_url)
                current_hash = hashlib.sha256(content).hexdigest()
            except FileNotFoundError:
                pass  # Storage answered: the file is gone. Judged below.
            except Exception as e:
                log.error("backup.live.read_failed", document_id=document_id, error=str(e))
                return VERIFY_UNAVAILABLE

        if current_hash == onchain_hash:
            return VERIFY_AUTHENTIC

        if snapshot.file_sha256 != onchain_hash:
            status = VERIFY_SNAPSHOT_COMPROMISED
        elif current_hash is None:
            status = VERIFY_FILE_MISSING
        else:
            status = VERIFY_TAMPERED
        log.error("backup.live.mismatch", document_id=document_id, status=status)
        await self._alert_live(document, snapshot, status, onchain_hash, current_hash)
        return status

    async def _alert_live(
        self,
        document: Document,
        snapshot: DocumentSnapshot,
        status: str,
        onchain_hash: str,
        current_hash: str | None,
    ) -> None:
        """Record the finding and notify the lawyer, once per tampered file.

        Only this check's own findings count as already alerted. A row from
        someone opening the document sent no alert, and an outage row also has no
        `current_hash`, so neither may hide a finding.
        """
        latest = await self.verification_repo.latest_scheduled_finding(document.id)
        if latest is not None and latest.current_hash == current_hash:
            return

        anchored = await self.onchain_repo.get_by_document_id(document.id)
        self.session.add(
            VerificationResult(
                document_id=document.id,
                verified_by=None,
                verified_at=datetime.now(timezone.utc),
                status=status,
                is_authentic=False,
                baseline_trusted=snapshot.file_sha256 == onchain_hash,
                onchain_hash=onchain_hash,
                snapshot_hash=snapshot.file_sha256,
                current_hash=current_hash,
                tx_hash=anchored.tx_hash if anchored else None,
                tamper_report_status="NOT_APPLICABLE",
            )
        )
        title, body = _LIVE_ALERTS[status]
        await self.notifications.create(
            # `finalized_by` is cleared when that user is deleted.
            user_id=document.finalized_by or document.user_id,
            type=NotificationType.LIVE_FILE_TAMPERED,
            title=title,
            body=body.format(file_name=document.file_name),
            event_metadata={"document_id": str(document.id), "status": status},
        )

    async def _repair(self, snapshot: DocumentSnapshot) -> BackupStatus:
        if not await self._anchored(snapshot):
            return "unrecoverable"
        copy = await self.session.get(SignedCopy, snapshot.signed_copy_id)
        try:
            assert copy is not None
            content = await get_storage_client().download(copy.storage_url)
        except Exception as e:
            log.error(
                "backup.repair.download_failed",
                document_id=str(snapshot.document_id),
                error=str(e),
            )
            return "unrecoverable"
        if not matches_snapshot(snapshot, content):
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
        if await self._onchain_hash(snapshot) != snapshot.file_sha256:
            log.error(
                "backup.repair.snapshot_not_anchored",
                document_id=str(snapshot.document_id),
            )
            return False
        return True

    async def _onchain_hash(self, snapshot: DocumentSnapshot) -> str | None:
        """The hash anchored for this document. None if there is no record or the
        chain could not be read.
        """
        document_id = str(snapshot.document_id)
        if self.chain is None:
            log.error("backup.chain.no_reader", document_id=document_id)
            return None
        try:
            record = await self.chain.get_document_hash(document_id=document_id)
        except Exception as e:
            log.error("backup.chain.read_failed", document_id=document_id, error=str(e))
            return None
        if record is None:
            log.error("backup.chain.no_record", document_id=document_id)
            return None
        return _normalise(record.data_hash)
