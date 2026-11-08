"""Alerts on every use of the contract owner key.

`correctDocumentHash` replaces an anchor, and a replaced hash becomes the value
every other check trusts, so nothing else in the app can notice it. The key's
events can't be hidden, so this reads them. The app can't tell a deliberate
correction from an attack: it alerts on both, and a person dismisses a real one.

Each run rescans the full history. A stored cursor would be one more database
value an attacker could move.
"""

from app.core import settings
from app.features.notifications.models import NotificationType
from app.features.notifications.repository import NotificationRepository
from app.features.notifications.service import NotificationService
from app.features.snapshot.records import record_anchor
from app.features.users.repository import UserRepository
from app.infrastructure.blockchain import BlockchainProvider, OwnerEvent
from app.infrastructure.observability.logging_setup import log
from app.shared.constants import ROLE_LAWYER
from app.shared.errors.exceptions import ServiceUnavailableError
from app.infrastructure.backup.alerts import send_backup_alert
from sqlalchemy.ext.asyncio import AsyncSession

from .schemas import OwnerWatchResponse

_ZERO_ADDRESS = "0x" + "00" * 20


class OwnerKeyWatch:
    def __init__(self, session: AsyncSession, chain: BlockchainProvider) -> None:
        self.session = session
        self.chain = chain
        self.notifications = NotificationService(session)
        self.notification_repo = NotificationRepository(session)
        self.user_repo = UserRepository(session)

    async def run(self) -> OwnerWatchResponse:
        """A chain that can't be read in full raises, so that nothing is alerted
        from a partial list."""
        if settings.CHAIN_SCAN_FROM_BLOCK is None:
            log.error("chain.owner_watch.not_configured")
            return OwnerWatchResponse(skipped=True)

        try:
            events = await self.chain.list_owner_events(settings.CHAIN_SCAN_FROM_BLOCK)
        except ServiceUnavailableError:
            log.error("chain.owner_watch.scan_failed")
            raise

        events = self._without_deployment(events)
        alerted = {
            metadata.get("tx_hash")
            for metadata in await self.notification_repo.get_metadata_by_type(
                NotificationType.ANCHOR_CORRECTED
            )
        }
        unalerted: dict[str, OwnerEvent] = {}
        for event in events:
            if event.tx_hash not in alerted:
                unalerted.setdefault(event.tx_hash, event)

        if unalerted:
            lawyers = await self.user_repo.list_ids_by_role(ROLE_LAWYER)
            for event in unalerted.values():
                title, body = _describe(event)
                for user_id in lawyers:
                    await self.notifications.create(
                        user_id=user_id,
                        type=NotificationType.ANCHOR_CORRECTED,
                        title=title,
                        body=body,
                        event_metadata={
                            "tx_hash": event.tx_hash,
                            "kind": event.kind,
                            "document_id": event.document_id,
                            "previous_hash": event.previous_hash,
                            "new_hash": event.new_hash,
                        },
                    )
            await self.session.commit()
            for event in unalerted.values():
                await self._email(*_describe(event))

        log_fn = log.warning if unalerted else log.info
        log_fn("chain.owner_watch.done", events=len(events), alerted=len(unalerted))
        return OwnerWatchResponse(events=len(events), alerted=len(unalerted))

    def _without_deployment(self, events: list[OwnerEvent]) -> list[OwnerEvent]:
        """Drop the constructor's events, and the server wallet's first
        authorization. Any later one means it was removed, so it counts."""
        deployment = {
            e.tx_hash
            for e in events
            if e.kind == "ownership_transferred" and e.previous_owner == _ZERO_ADDRESS
        }
        wallet = self.chain.recorder_address.lower()

        def authorizes_wallet(event: OwnerEvent) -> bool:
            return bool(
                event.kind == "recorder_set"
                and event.authorized
                and (event.recorder or "").lower() == wallet
            )

        wallet_set = any(
            authorizes_wallet(e) for e in events if e.tx_hash in deployment
        )
        kept: list[OwnerEvent] = []
        for event in events:
            if event.tx_hash in deployment:
                continue
            if authorizes_wallet(event) and not wallet_set:
                wallet_set = True
                continue
            kept.append(event)
        return kept

    @staticmethod
    async def _email(subject: str, body: str) -> None:
        try:
            await send_backup_alert(subject, body)
        except Exception as e:
            log.error("chain.owner_watch.email_failed", error=str(e))


def _describe(event: OwnerEvent) -> tuple[str, str]:
    link = f"{settings.CHAIN_EXPLORER_TX_URL}0x{event.tx_hash.removeprefix('0x')}"
    if event.kind == "correction":
        anchor_id = event.document_id or ""
        document_id, record = record_anchor(anchor_id) or (anchor_id, "PDF")
        return (
            "Anchor replaced with the owner key",
            (
                f"The {record} anchor of document {document_id} was replaced. "
                f"Previous hash: {event.previous_hash}. New hash: {event.new_hash}. "
                f"Reason given: {event.reason}. Every check now trusts the new "
                f"hash; if this wasn't deliberate, reverting it is a manual "
                f"decision using the previous hash. Transaction: {link}"
            ),
        )
    if event.kind == "recorder_set":
        action = "authorized" if event.authorized else "removed"
        return (
            "Contract recorder changed",
            f"Recorder {event.recorder} was {action} with the owner key. Transaction: {link}",
        )
    return (
        "Contract ownership transferred",
        (
            f"Ownership moved from {event.previous_owner} to {event.new_owner}. "
            f"Transaction: {link}"
        ),
    )
