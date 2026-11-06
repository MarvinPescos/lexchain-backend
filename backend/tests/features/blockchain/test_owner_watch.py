"""Alerting on every use of the contract owner key."""

import pytest
from sqlalchemy import select

from app.core import settings
from app.features.blockchain.owner_watch import OwnerKeyWatch
from app.features.blockchain.service import BlockchainService
from app.features.documents.workflow.finalization import DocumentFinalization
from app.features.notifications.models import Notification, NotificationType
from app.infrastructure.blockchain.base import OwnerEvent
from app.shared.errors.exceptions import ServiceUnavailableError
from tests.conftest import as_auth, mark_processed, pdf

ZERO = "0x" + "00" * 20
OTHER = "0x" + "bb" * 20
OLD, NEW = "11" * 32, "22" * 32


def correction(document_id: str, tx: str = "aa") -> OwnerEvent:
    return OwnerEvent(
        kind="correction",
        tx_hash=tx * 32,
        block_number=5,
        document_id=document_id,
        previous_hash=OLD,
        new_hash=NEW,
        reason="typo",
    )


@pytest.fixture(autouse=True)
def scan_from_block(monkeypatch) -> None:
    monkeypatch.setattr(settings, "CHAIN_SCAN_FROM_BLOCK", 100)


@pytest.fixture
def emails(monkeypatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []

    async def record(subject, body):
        sent.append((subject, body))

    monkeypatch.setattr("app.features.blockchain.owner_watch.send_backup_alert", record)
    return sent


@pytest.fixture
def watch(session, chain, emails) -> OwnerKeyWatch:
    return OwnerKeyWatch(session, chain)


async def _alerts(session) -> list[Notification]:
    result = await session.execute(
        select(Notification).where(Notification.type == NotificationType.ANCHOR_CORRECTED)
    )
    return list(result.scalars().all())


@pytest.mark.parametrize(
    ("anchor_id", "record"),
    [("doc-1", "PDF anchor"), ("doc-1:metadata", "metadata record anchor")],
)
async def test_a_correction_alerts_once_naming_the_old_and_new_hash(
    anchor_id, record, session, chain, watch, lawyer, other_lawyer, emails
):
    chain.owner_events = [correction(anchor_id)]

    await watch.run()
    report = await watch.run()

    assert (report.events, report.alerted) == (1, 0)
    alerts = await _alerts(session)
    assert {a.user_id for a in alerts} == {lawyer.id, other_lawyer.id}
    assert len(alerts) == 2
    body = alerts[0].body
    assert record in body and "doc-1 " in body
    assert OLD in body and NEW in body and "typo" in body
    assert f"{settings.CHAIN_EXPLORER_TX_URL}0x{'aa' * 32}" in body
    assert len(emails) == 1


async def test_a_transferred_owner_alerts_once(session, chain, watch, lawyer):
    chain.owner_events = [
        OwnerEvent(
            kind="ownership_transferred",
            tx_hash="cc" * 32,
            block_number=5,
            previous_owner=chain.recorder_address,
            new_owner=OTHER,
        )
    ]

    await watch.run()
    await watch.run()

    [alert] = await _alerts(session)
    assert OTHER in alert.body


async def test_a_newly_authorized_recorder_alerts_once(session, chain, watch, lawyer):
    chain.owner_events = [
        OwnerEvent(
            kind="recorder_set",
            tx_hash="dd" * 32,
            block_number=5,
            recorder=OTHER,
            authorized=True,
        )
    ]

    await watch.run()
    await watch.run()

    [alert] = await _alerts(session)
    assert OTHER in alert.body


async def test_deployment_events_raise_nothing(session, chain, watch, lawyer):
    deploy = "ee" * 32
    chain.owner_events = [
        OwnerEvent(
            kind="ownership_transferred",
            tx_hash=deploy,
            block_number=1,
            previous_owner=ZERO,
            new_owner=OTHER,
        ),
        OwnerEvent(
            kind="recorder_set",
            tx_hash=deploy,
            block_number=1,
            recorder=OTHER,
            authorized=True,
        ),
        OwnerEvent(
            kind="recorder_set",
            tx_hash="ff" * 32,
            block_number=2,
            recorder=chain.recorder_address.upper().replace("0X", "0x"),
            authorized=True,
        ),
    ]

    report = await watch.run()

    assert report.events == 0
    assert await _alerts(session) == []


async def test_authorizing_the_server_wallet_again_later_alerts(
    session, chain, watch, lawyer
):
    deploy = "ee" * 32
    chain.owner_events = [
        OwnerEvent(
            kind="ownership_transferred",
            tx_hash=deploy,
            block_number=1,
            previous_owner=ZERO,
            new_owner=chain.recorder_address,
        ),
        OwnerEvent(
            kind="recorder_set",
            tx_hash=deploy,
            block_number=1,
            recorder=chain.recorder_address,
            authorized=True,
        ),
        OwnerEvent(
            kind="recorder_set",
            tx_hash="ab" * 32,
            block_number=9,
            recorder=chain.recorder_address,
            authorized=True,
        ),
    ]

    await watch.run()

    assert len(await _alerts(session)) == 1


async def test_removing_the_server_wallet_alerts(session, chain, watch, lawyer):
    chain.owner_events = [
        OwnerEvent(
            kind="recorder_set",
            tx_hash="ab" * 32,
            block_number=2,
            recorder=chain.recorder_address,
            authorized=False,
        )
    ]

    await watch.run()

    assert len(await _alerts(session)) == 1


async def test_a_failed_scan_raises_and_alerts_nothing(
    session, chain, watch, lawyer, monkeypatch
):
    async def partial(from_block):
        raise ServiceUnavailableError("chunk failed")

    monkeypatch.setattr(chain, "list_owner_events", partial)

    with pytest.raises(ServiceUnavailableError):
        await watch.run()

    assert await _alerts(session) == []


async def test_watch_is_skipped_without_a_scan_block(chain, watch, monkeypatch):
    monkeypatch.setattr(settings, "CHAIN_SCAN_FROM_BLOCK", None)

    assert (await watch.run()).skipped is True


def test_the_task_is_scheduled_on_a_queue_a_worker_consumes():
    from app.celery_app import celery_app

    celery_app.loader.import_default_modules()

    schedule = celery_app.conf.beat_schedule["watch-owner-events"]
    assert schedule["task"] == "chain.watch_owner_events"
    assert schedule["task"] in celery_app.tasks
    route = celery_app.amqp.router.route({}, "chain.watch_owner_events")
    assert route["queue"].name == "backups"


# === On verify ===


@pytest.fixture
async def document_id(session, intake, queue, chain, storage, lawyer, open_book):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    await DocumentFinalization(session, chain).finalize(me, doc.document_id)
    return doc.document_id


@pytest.mark.parametrize("suffix", ["", ":metadata"])
async def test_verify_shows_a_corrected_anchor(
    suffix, session, chain, storage, document_id, lawyer
):
    chain.owner_events = [correction(f"{document_id}{suffix}")]

    result = await BlockchainService(session, chain).verify_document(
        as_auth(lawyer), document_id
    )

    assert result.status == "AUTHENTIC"
    assert result.anchor_corrected is True
    assert "replaced" in result.message


async def test_verify_shows_an_uncorrected_anchor(session, chain, storage, document_id, lawyer):
    result = await BlockchainService(session, chain).verify_document(
        as_auth(lawyer), document_id
    )

    assert result.anchor_corrected is False


async def test_a_failed_event_read_is_unknown_not_uncorrected(
    session, chain, storage, document_id, lawyer, monkeypatch
):
    async def down(from_block):
        raise ServiceUnavailableError("rpc unreachable")

    monkeypatch.setattr(chain, "corrected_document_ids", down)

    result = await BlockchainService(session, chain).verify_document(
        as_auth(lawyer), document_id
    )

    assert result.status == "AUTHENTIC"
    assert result.anchor_corrected is None
