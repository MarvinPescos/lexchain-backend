"""Checks the database against what the chain says should exist.

Every other integrity check starts from the database's rows, so a row deleted
outright is never looked at. This one starts from the chain, and checks presence
only: comparing hashes is already covered by verify and `SnapshotArchive`.

Each run rescans the full history. A stored cursor would be one more database
value an attacker could move.
"""

from typing import get_args

from app.core import settings
from app.features.documents.constants import LIFECYCLE_FINALIZED
from app.features.notifications.models import NotificationType
from app.features.notifications.repository import NotificationRepository
from app.features.notifications.service import NotificationService
from app.features.snapshot.records import is_metadata_anchor
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.features.users.repository import UserRepository
from app.infrastructure.blockchain import BlockchainProvider
from app.infrastructure.observability.logging_setup import log
from app.shared.constants import ROLE_LAWYER
from app.shared.errors.exceptions import ServiceUnavailableError
from sqlalchemy.ext.asyncio import AsyncSession

from .schemas import ChainReconcileCheck, ChainReconcileResponse, ChainReconcileStatus

# No alert for an orphaned anchor: a failed finalize can leave one with no attack.
_ALERTS: dict[ChainReconcileStatus, tuple[str, str]] = {
    "document_missing": (
        "Document record missing",
        (
            "Document {document_id} is anchored on the blockchain, but its record "
            "is gone from the database. Restore it from a database backup."
        ),
    ),
    "snapshot_missing": (
        "Document snapshot missing",
        (
            "Document {document_id} is anchored on the blockchain, but its snapshot "
            "is gone from the database. Restore it from a database backup."
        ),
    ),
}


class ChainReconciliation:
    def __init__(self, session: AsyncSession, chain: BlockchainProvider) -> None:
        self.session = session
        self.chain = chain
        self.notifications = NotificationService(session)
        self.notification_repo = NotificationRepository(session)
        self.snapshot_repo = DocumentSnaphotRepository(session)
        self.user_repo = UserRepository(session)

    async def run(self) -> ChainReconcileResponse:
        """Report every anchored ID the database can't account for.

        A chain that can't be read in full raises, so that nothing is reported
        from a partial list.
        """
        if settings.CHAIN_SCAN_FROM_BLOCK is None:
            log.error("chain.reconcile.not_configured")
            return ChainReconcileResponse(skipped=True)

        try:
            anchored = await self.chain.list_anchored_document_ids(
                settings.CHAIN_SCAN_FROM_BLOCK
            )
        except ServiceUnavailableError:
            log.error("chain.reconcile.scan_failed")
            raise
        # A metadata anchor belongs to a document; it isn't a document itself.
        anchored = {a for a in anchored if not is_metadata_anchor(a)}

        # Read after the scan, so an anchor made during it finds its row.
        rows = {
            str(document_id): (lifecycle, snapshot_id is not None)
            for document_id, lifecycle, snapshot_id in (
                await self.snapshot_repo.list_document_presence()
            )
        }
        counts = {s: 0 for s in get_args(ChainReconcileStatus)}
        problems: list[ChainReconcileCheck] = []
        for document_id in sorted(anchored):
            status = self._status(rows.get(document_id))
            counts[status] += 1
            if status != "ok":
                log_fn = log.warning if status == "orphaned_anchor" else log.error
                log_fn(f"chain.reconcile.{status}", document_id=document_id)
                problems.append(
                    ChainReconcileCheck(document_id=document_id, status=status)
                )

        await self._alert(problems)

        log_fn = log.warning if problems else log.info
        log_fn("chain.reconcile.done", checked=len(anchored), **counts)
        return ChainReconcileResponse(
            checked=len(anchored),
            ok=counts["ok"],
            snapshot_missing=counts["snapshot_missing"],
            orphaned_anchor=counts["orphaned_anchor"],
            document_missing=counts["document_missing"],
            problems=problems,
        )

    @staticmethod
    def _status(row: tuple[str, bool] | None) -> ChainReconcileStatus:
        if row is None:
            return "document_missing"
        lifecycle, has_snapshot = row
        if lifecycle != LIFECYCLE_FINALIZED:
            return "orphaned_anchor"
        return "ok" if has_snapshot else "snapshot_missing"

    async def _alert(self, problems: list[ChainReconcileCheck]) -> None:
        """Notify every lawyer of each finding not yet alerted.

        The document's own `finalized_by` may be gone with its row, and there is
        no `verification_results` row to deduplicate on, so past alerts are
        found through their metadata.
        """
        findings = [p for p in problems if p.status in _ALERTS]
        if not findings:
            return

        alerted = {
            (metadata.get("document_id"), metadata.get("finding"))
            for metadata in await self.notification_repo.get_metadata_by_type(
                NotificationType.ANCHORED_RECORD_MISSING
            )
        }
        unalerted = [p for p in findings if (p.document_id, p.status) not in alerted]
        if not unalerted:
            return

        lawyers = await self.user_repo.list_ids_by_role(ROLE_LAWYER)
        for finding in unalerted:
            title, body = _ALERTS[finding.status]
            for user_id in lawyers:
                await self.notifications.create(
                    user_id=user_id,
                    type=NotificationType.ANCHORED_RECORD_MISSING,
                    title=title,
                    body=body.format(document_id=finding.document_id),
                    event_metadata={
                        "document_id": finding.document_id,
                        "finding": finding.status,
                    },
                )
        await self.session.commit()
