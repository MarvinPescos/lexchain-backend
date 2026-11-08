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
per tampered file; a read failure is unknown and neither. The same pass checks
the metadata, analysis, snapshot and lifecycle (see `records`), and alerts on a lost or
bad archive, because nobody otherwise learns of it until they need it.
"""

import hashlib
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from typing import get_args

from app.features.blockchain.constants import (
    VERIFY_AUTHENTIC,
    VERIFY_FILE_MISSING,
    VERIFY_RECORD_ALTERED,
    VERIFY_SNAPSHOT_COMPROMISED,
    VERIFY_TAMPERED,
    VERIFY_UNAVAILABLE,
)
from app.features.blockchain.models import VerificationResult
from app.features.blockchain.repository import (
    OnChainRecordRepository,
    VerificationResultRepository,
)
from app.features.documents.constants import LIFECYCLE_FINALIZED
from app.features.documents.models import Document, SignedCopy
from app.features.notifications.models import NotificationType
from app.features.notifications.repository import NotificationRepository
from app.features.notifications.service import NotificationService
from app.features.users.models import User
from app.infrastructure.backup import BackupProvider
from app.infrastructure.blockchain import BlockchainProvider
from app.infrastructure.clients.supabase_storage import get_storage_client
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.backup.alerts import send_backup_alert
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import DocumentSnapshot
from .records import (
    document_hash_change,
    MetadataCheck,
    RecordCheck,
    analysis_anchor_id,
    analysis_key,
    archive_key,
    metadata_anchor_id,
    metadata_key,
    normalise_hash,
    sha256_hex,
)
from .schemas import (
    BackupCheck,
    BackupStatus,
    BackupVerificationResponse,
    LiveFileCheck,
    LiveFileReport,
    RecordChange,
    RecordFinding,
    RecordKind,
    RecordReport,
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


_ARCHIVE_ALERTS: dict[str, tuple[str, str]] = {
    "missing": ("Recovery copy missing", "The archived original of {file_name} is gone."),
    "mismatch": (
        "Recovery copy damaged",
        "The archived original of {file_name} no longer matches the hash anchored on-chain.",
    ),
    "unrecoverable": (
        "Recovery copy lost",
        (
            "The archived original of {file_name} is gone or damaged, and the live "
            "copy can't replace it."
        ),
    ),
}
_METADATA_ARCHIVE_ALERTS: dict[str, tuple[str, str]] = {
    "archive_missing": (
        "Recovery copy of the record missing",
        "The archived metadata record of {file_name} is gone.",
    ),
    "archive_mismatch": (
        "Recovery copy of the record damaged",
        "The archived metadata record of {file_name} no longer matches its anchor.",
    ),
}
_ANALYSIS_ARCHIVE_ALERTS: dict[str, tuple[str, str]] = {
    "archive_missing": (
        "Recovery copy of the analysis missing",
        "The archived analysis record of {file_name} is gone.",
    ),
    "archive_mismatch": (
        "Recovery copy of the analysis damaged",
        "The archived analysis record of {file_name} no longer matches its anchor.",
    ),
}
# Each document holds two anchored records, counted apart so one record's
# outcome never hides the other's.
RECORDS_PER_DOCUMENT = 2
_ARCHIVE_ALERT_ADVICE = " Restore won't work for this document until the archive is back."


def backup_key(snapshot: DocumentSnapshot) -> str:
    return archive_key(snapshot.document_id, snapshot.file_sha256)


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
        self.records = RecordCheck(session, chain, backup) if chain else None
        self.notifications = NotificationService(session)
        self.notification_repo = NotificationRepository(session)
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

    async def archive_metadata(self, snapshot: DocumentSnapshot, record: bytes) -> bool:
        """Store the canonical metadata record beside the PDF, then read it back
        and check it. Its name is its hash, so it is never overwritten.
        """
        record_hash = await self._archive_record("metadata", snapshot, record, metadata_key)
        if record_hash is None:
            return False
        snapshot.metadata_sha256 = record_hash
        return True

    async def archive_analysis(self, snapshot: DocumentSnapshot, record: bytes) -> bool:
        """The same for the analysis record."""
        record_hash = await self._archive_record("analysis", snapshot, record, analysis_key)
        if record_hash is None:
            return False
        snapshot.analysis_sha256 = record_hash
        return True

    async def _archive_record(
        self,
        name: str,
        snapshot: DocumentSnapshot,
        record: bytes,
        key_for: Callable[[uuid.UUID, str], str],
    ) -> str | None:
        """The record's hash once it is stored and reads back intact."""
        record_hash = sha256_hex(record)
        key = key_for(snapshot.document_id, record_hash)
        try:
            if not await self.backup.exists(key):
                await self.backup.store(key, record, "application/json")
            stored = await self.backup.fetch(key)
        except Exception as e:
            log.error(
                f"backup.archive.{name}_failed",
                document_id=str(snapshot.document_id),
                key=key,
                error=str(e),
            )
            return None
        if stored != record:
            log.error(
                f"backup.archive.{name}_readback_mismatch",
                document_id=str(snapshot.document_id),
                key=key,
            )
            return None
        return record_hash

    async def anchor_pending_metadata(self) -> int:
        """Anchor archived metadata and analysis records finalize couldn't.
        Returns how many.

        The hash comes from the archived JSON, never the live rows, which may
        have changed since. One document's failure doesn't stop the rest.
        """
        assert self.chain is not None
        result = await self.session.execute(
            select(DocumentSnapshot)
            .where(
                DocumentSnapshot.metadata_sha256.is_not(None)
                | DocumentSnapshot.analysis_sha256.is_not(None)
            )
            .order_by(DocumentSnapshot.created_at)
        )
        anchored = 0
        for snapshot in result.scalars().all():
            for anchor_id, key, record_hash in (
                (
                    metadata_anchor_id(snapshot.document_id),
                    metadata_key,
                    snapshot.metadata_sha256,
                ),
                (
                    analysis_anchor_id(snapshot.document_id),
                    analysis_key,
                    snapshot.analysis_sha256,
                ),
            ):
                if record_hash is None:
                    continue
                try:
                    anchored += await self._anchor_record(
                        snapshot, anchor_id, key(snapshot.document_id, record_hash), record_hash
                    )
                except Exception as e:
                    log.error(
                        "backup.record_anchor.failed",
                        document_id=str(snapshot.document_id),
                        anchor_id=anchor_id,
                        error=str(e),
                    )
        return anchored

    async def _anchor_record(
        self, snapshot: DocumentSnapshot, anchor_id: str, key: str, record_hash: str
    ) -> bool:
        assert self.chain is not None
        if await self._record_anchored(anchor_id, record_hash):
            return False
        data = await self.backup.fetch(key)
        data_hash = sha256_hex(data)
        if data_hash != record_hash:
            log.error(
                "backup.record_anchor.archive_mismatch",
                document_id=str(snapshot.document_id),
                anchor_id=anchor_id,
            )
            return False
        try:
            await self.chain.store_document_hash(anchor_id, data_hash)
        except Exception:
            # The contract refuses a second write, so an anchor from a run that
            # lost its answer lands here. It's done if it holds this hash.
            if not await self._record_anchored(anchor_id, data_hash):
                raise
            return False
        log.info(
            "backup.record_anchor.done",
            document_id=str(snapshot.document_id),
            anchor_id=anchor_id,
        )
        return True

    async def _record_anchored(self, anchor_id: str, expected: str) -> bool:
        assert self.chain is not None
        record = await self.chain.get_document_hash(anchor_id, retry_empty=False)
        return record is not None and normalise_hash(record.data_hash) == expected

    async def verify(self, repair: bool = False) -> BackupVerificationResponse:
        """Hash every archive against the snapshot and the hash read from the chain.

        An archive the chain could not confirm is `unverified`: integrity unknown,
        never repaired. With `repair`, a missing archive is written from the live signed copy —
        only when the snapshot matches the chain and the live copy matches the
        snapshot. A bad archive is reported, never overwritten.

        Every live file is checked too, whatever its archive's status, and so
        are the records, even when the file is authentic. When the chain can't be
        read, nothing is recorded or alerted.
        """
        result = await self.session.execute(
            select(DocumentSnapshot).order_by(DocumentSnapshot.created_at)
        )
        snapshots = list(result.scalars().all())

        counts = {s: 0 for s in BACKUP_STATUSES}
        problems: list[BackupCheck] = []
        live_counts: Counter[str] = Counter()
        live_problems: list[LiveFileCheck] = []
        record_counts: Counter[str] = Counter()
        record_problems: list[RecordFinding] = []
        record_alerted = await self._alerted(NotificationType.RECORD_ALTERED)
        archive_alerted = await self._alerted(NotificationType.ARCHIVE_PROBLEM)
        emails: list[tuple[str, str]] = []
        for snapshot in snapshots:
            onchain_hash = await self._onchain_hash(snapshot)
            status, archived = await self._check(snapshot, onchain_hash)
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
            if onchain_hash is None or self.records is None:
                record_counts["unavailable"] += RECORDS_PER_DOCUMENT
                continue
            document = await self.session.get(Document, snapshot.document_id)
            assert document is not None  # the snapshot's foreign key
            metadata = await self.records.metadata(document, check_archive=True)
            analysis = await self.records.analysis(document, check_archive=True)
            findings = await self._record_findings(
                document, snapshot, onchain_hash, archived, metadata, analysis
            )
            record_problems += findings
            record_counts[
                self._record_outcome(
                    [f for f in findings if f.kind != "analysis"], metadata
                )
            ] += 1
            record_counts[
                self._record_outcome(
                    [f for f in findings if f.kind == "analysis"], analysis
                )
            ] += 1
            recipient = await self._recipient(document, metadata)
            await self._alert_records(
                document,
                snapshot,
                findings,
                onchain_hash,
                live_status,
                recipient,
                record_alerted,
            )
            for finding, alert in (
                (status, _ARCHIVE_ALERTS.get(status)),
                (metadata.problem, _METADATA_ARCHIVE_ALERTS.get(metadata.problem or "")),
                (
                    f"analysis_{analysis.problem}",
                    _ANALYSIS_ARCHIVE_ALERTS.get(analysis.problem or ""),
                ),
            ):
                if alert is not None:
                    email = await self._alert_archive(
                        document, str(finding), alert, recipient, archive_alerted
                    )
                    if email:
                        emails.append(email)
        await self.session.commit()
        for subject, body in emails:
            try:
                await send_backup_alert(subject, body)
            except Exception as e:
                log.error("backup.alert.email_failed", error=str(e))

        live = LiveFileReport(
            checked=len(snapshots),
            ok=live_counts[VERIFY_AUTHENTIC],
            tampered=live_counts[VERIFY_TAMPERED],
            file_missing=live_counts[VERIFY_FILE_MISSING],
            snapshot_compromised=live_counts[VERIFY_SNAPSHOT_COMPROMISED],
            unavailable=live_counts[VERIFY_UNAVAILABLE],
            problems=live_problems,
        )
        records = RecordReport(
            checked=len(snapshots) * RECORDS_PER_DOCUMENT,
            ok=record_counts["ok"],
            altered=record_counts["altered"],
            not_anchored=record_counts["not_anchored"],
            unavailable=record_counts["unavailable"],
            problems=record_problems,
        )
        log_fn = log.warning if problems or live_problems or record_problems else log.info
        log_fn(
            "backup.verify.done",
            checked=len(snapshots),
            repair=repair,
            live=dict(live_counts),
            records=dict(record_counts),
            **counts,
        )
        return BackupVerificationResponse(
            checked=len(snapshots),
            problems=problems,
            live=live,
            records=records,
            **counts,
        )

    async def _check(
        self, snapshot: DocumentSnapshot, onchain_hash: str | None
    ) -> tuple[BackupStatus, bytes | None]:
        """The archive's status, and its bytes if the chain proved them.

        The chain hash picks the archive and judges it, so an edited snapshot
        row is a record finding and not also a bad archive. Only when the chain
        can't be read does the row stand in, and then nothing counts as good.
        """
        key = (
            archive_key(snapshot.document_id, onchain_hash)
            if onchain_hash is not None
            else snapshot.backup_key
        )
        if key is None:
            return "missing", None
        document_id = str(snapshot.document_id)
        try:
            # Asked first: a fetch error alone can't tell lost from unreachable.
            if not await self.backup.exists(key):
                log.error("backup.verify.not_found", document_id=document_id, key=key)
                return "missing", None
            data = await self.backup.fetch(key)
        except Exception as e:
            log.error(
                "backup.verify.fetch_failed",
                document_id=document_id,
                key=key,
                error=str(e),
            )
            return "unavailable", None
        if onchain_hash is None:
            if not matches_snapshot(snapshot, data):
                log.error("backup.verify.mismatch", document_id=document_id, key=key)
                return "mismatch", None
            return "unverified", None
        if sha256_hex(data) != onchain_hash:
            log.error("backup.verify.chain_mismatch", document_id=document_id, key=key)
            return "mismatch", None
        return "ok", data

    async def _alerted(self, type: NotificationType) -> set[tuple]:
        """What was already alerted. A record finding can sit on an authentic
        file, so `latest_scheduled_finding` can't answer this. An alert from
        before a restore doesn't count: the same edit again is a new tampering.
        """
        restores = await self.verification_repo.last_restores()
        alerted: set[tuple] = set()
        for metadata, sent_at in await self.notification_repo.get_sent_by_type(type):
            document_id = metadata.get("document_id")
            restored_at = restores.get(document_id)
            if restored_at is None or sent_at > restored_at:
                alerted.add((document_id, metadata.get("finding"), metadata.get("value")))
        return alerted

    async def _record_findings(
        self,
        document: Document,
        snapshot: DocumentSnapshot,
        onchain_hash: str,
        archived: bytes | None,
        metadata: MetadataCheck,
        analysis: MetadataCheck,
    ) -> list[RecordFinding]:
        assert self.records is not None
        changes: list[tuple[RecordKind, RecordChange]] = []
        if document.lifecycle != LIFECYCLE_FINALIZED:
            changes.append(
                (
                    "lifecycle",
                    RecordChange(
                        field="lifecycle",
                        anchored=LIFECYCLE_FINALIZED,
                        current=document.lifecycle,
                    ),
                )
            )
        hash_change = document_hash_change(document, onchain_hash)
        if hash_change is not None:
            changes.append(("document", hash_change))
        changes += [
            ("snapshot", change)
            for change in await self.records.snapshot_changes(
                snapshot, onchain_hash, archived
            )
        ]
        changes += [("metadata", change) for change in metadata.changes]
        changes += [("analysis", change) for change in analysis.changes]
        return [
            RecordFinding(document_id=document.id, kind=kind, **change.model_dump())
            for kind, change in changes
        ]

    @staticmethod
    def _record_outcome(findings: list[RecordFinding], metadata: MetadataCheck) -> str:
        if findings:
            return "altered"
        if metadata.status == "UNAVAILABLE":
            return "unavailable"
        return "not_anchored" if metadata.status == "NOT_ANCHORED" else "ok"

    async def _recipient(
        self, document: Document, metadata: MetadataCheck
    ) -> uuid.UUID:
        """`finalized_by`, falling back to the owner, as anchored if known: an
        edit to either column must not send the alert to the one who made it.
        """
        anchored = metadata.anchored or {}
        for candidate in (anchored.get("finalized_by"), anchored.get("user_id")):
            if candidate and await self.session.get(User, uuid.UUID(candidate)):
                return uuid.UUID(candidate)
        # `finalized_by` is cleared when that user is deleted.
        return document.finalized_by or document.user_id

    async def _alert_records(
        self,
        document: Document,
        snapshot: DocumentSnapshot,
        findings: list[RecordFinding],
        onchain_hash: str,
        live_status: str,
        recipient: uuid.UUID,
        alerted: set[tuple],
    ) -> None:
        """Record and notify each finding, once per altered value."""
        for finding in findings:
            key = (
                str(document.id),
                f"{finding.kind}:{finding.field}",
                str(finding.current),
            )
            if key in alerted:
                continue
            alerted.add(key)
            self.session.add(
                VerificationResult(
                    document_id=document.id,
                    verified_by=None,
                    verified_at=datetime.now(timezone.utc),
                    status=VERIFY_RECORD_ALTERED,
                    is_authentic=live_status == VERIFY_AUTHENTIC,
                    baseline_trusted=snapshot.file_sha256 == onchain_hash,
                    onchain_hash=onchain_hash,
                    snapshot_hash=snapshot.file_sha256,
                    tamper_report_status="NOT_APPLICABLE",
                )
            )
            await self.notifications.create(
                user_id=recipient,
                type=NotificationType.RECORD_ALTERED,
                title="Document record altered",
                body=(
                    f"`{finding.field}` of {document.file_name} was changed from "
                    f"{finding.anchored} to {finding.current}."
                ),
                event_metadata={
                    "document_id": key[0],
                    "finding": key[1],
                    "value": key[2],
                },
            )

    async def _alert_archive(
        self,
        document: Document,
        finding: str,
        alert: tuple[str, str],
        recipient: uuid.UUID,
        alerted: set[tuple],
    ) -> tuple[str, str] | None:
        """Notify once per document and finding. Returns the email to send once
        the notification is committed."""
        key = (str(document.id), finding, None)
        if key in alerted:
            return None
        alerted.add(key)
        title, body = alert
        body = body.format(file_name=document.file_name) + _ARCHIVE_ALERT_ADVICE
        await self.notifications.create(
            user_id=recipient,
            type=NotificationType.ARCHIVE_PROBLEM,
            title=title,
            body=body,
            event_metadata={"document_id": key[0], "finding": finding},
        )
        return title, body

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
        return normalise_hash(record.data_hash)
