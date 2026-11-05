"""Reconciling the database against the document IDs anchored on-chain."""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from structlog.testing import capture_logs

from app.core import settings
from app.features.admin.router import reconcile_chain
from app.features.blockchain.reconciliation import ChainReconciliation
from app.features.documents.models import Document
from app.features.documents.workflow.finalization import DocumentFinalization
from app.features.notifications.models import Notification, NotificationType
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.infrastructure.blockchain.mock_provider import MockBlockchainProvider
from app.infrastructure.blockchain.web3_provider import Web3BlockchainProvider
from app.shared.errors.exceptions import ServiceUnavailableError
from tests.conftest import as_auth, mark_processed, pdf


@pytest.fixture(autouse=True)
def scan_from_block(monkeypatch) -> None:
    monkeypatch.setattr(settings, "CHAIN_SCAN_FROM_BLOCK", 100)


@pytest.fixture
def reconciliation(session, chain) -> ChainReconciliation:
    return ChainReconciliation(session, chain)


@pytest.fixture
async def finalized(session, intake, queue, chain, storage, lawyer, open_book) -> uuid.UUID:
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    await DocumentFinalization(session, chain).finalize(me, doc.document_id)
    return doc.document_id


async def _delete_snapshot(session, document_id) -> None:
    snapshot = await DocumentSnaphotRepository(session).get_by_document_id(document_id)
    await session.delete(snapshot)
    await session.flush()


async def _delete_document(session, document_id) -> None:
    """As an attacker would, in SQL; the snapshot's foreign key forces it to go first."""
    snapshot = await DocumentSnaphotRepository(session).get_by_document_id(document_id)
    if snapshot is not None:
        await _delete_snapshot(session, document_id)
    await session.execute(delete(Document).where(Document.id == document_id))
    session.expunge_all()


async def _alerts(session) -> list[Notification]:
    result = await session.execute(
        select(Notification).where(
            Notification.type == NotificationType.ANCHORED_RECORD_MISSING
        )
    )
    return list(result.scalars().all())


# === Findings ===


async def test_clean_database_reports_no_problems(session, reconciliation, finalized):
    report = await reconciliation.run()

    assert (report.checked, report.ok) == (1, 1)
    assert report.problems == []
    assert await _alerts(session) == []


async def test_deleted_document_row_is_reported(session, reconciliation, finalized):
    gone = str(finalized)
    await _delete_document(session, finalized)

    with capture_logs() as logs:
        report = await reconciliation.run()

    assert report.document_missing == 1
    assert [(p.document_id, p.status) for p in report.problems] == [
        (gone, "document_missing")
    ]
    assert any(e["event"] == "chain.reconcile.document_missing" for e in logs)


async def test_anchored_id_that_is_not_a_uuid_is_reported(reconciliation, chain):
    await chain.store_document_hash("not-a-uuid", "ab" * 32)

    report = await reconciliation.run()

    assert report.problems[0].document_id == "not-a-uuid"
    assert report.problems[0].status == "document_missing"


async def test_deleted_snapshot_row_is_reported(session, reconciliation, finalized):
    await _delete_snapshot(session, finalized)

    with capture_logs() as logs:
        report = await reconciliation.run()

    assert report.snapshot_missing == 1
    assert [(p.document_id, p.status) for p in report.problems] == [
        (str(finalized), "snapshot_missing")
    ]
    assert any(e["event"] == "chain.reconcile.snapshot_missing" for e in logs)


async def test_unfinalized_anchor_is_orphaned_and_sends_no_alert(
    session, reconciliation, finalized
):
    document = await session.get(Document, finalized)
    document.lifecycle = "SIGNED"
    await session.flush()

    with capture_logs() as logs:
        report = await reconciliation.run()

    assert report.orphaned_anchor == 1
    assert report.problems[0].status == "orphaned_anchor"
    assert any(e["event"] == "chain.reconcile.orphaned_anchor" for e in logs)
    assert await _alerts(session) == []


# === Alerts ===


async def test_every_lawyer_is_alerted(session, reconciliation, chain, lawyer, other_lawyer):
    gone = str(uuid.uuid4())
    await chain.store_document_hash(gone, "ab" * 32)

    await reconciliation.run()

    alerts = await _alerts(session)
    assert {a.user_id for a in alerts} == {lawyer.id, other_lawyer.id}
    assert all(gone in a.body for a in alerts)


async def test_clients_are_not_alerted(session, reconciliation, chain, lawyer, client):
    await chain.store_document_hash(str(uuid.uuid4()), "ab" * 32)

    await reconciliation.run()

    assert {a.user_id for a in await _alerts(session)} == {lawyer.id}


async def test_repeated_runs_alert_once_per_finding(session, reconciliation, chain, lawyer):
    await chain.store_document_hash(str(uuid.uuid4()), "ab" * 32)

    await reconciliation.run()
    second = await reconciliation.run()

    assert second.document_missing == 1
    assert len(await _alerts(session)) == 1


async def test_a_new_finding_on_the_same_document_alerts_again(
    session, reconciliation, finalized
):
    await _delete_snapshot(session, finalized)
    await reconciliation.run()

    await _delete_document(session, finalized)
    await reconciliation.run()

    findings = {a.event_metadata["finding"] for a in await _alerts(session)}
    assert findings == {"snapshot_missing", "document_missing"}


# === Runs that must not report ===


async def test_failed_scan_aborts_with_no_findings_or_alerts(
    session, reconciliation, chain, lawyer, monkeypatch
):
    await chain.store_document_hash(str(uuid.uuid4()), "ab" * 32)

    async def unreachable(from_block):
        raise ServiceUnavailableError("chain down")

    monkeypatch.setattr(chain, "list_anchored_document_ids", unreachable)

    with capture_logs() as logs, pytest.raises(ServiceUnavailableError):
        await reconciliation.run()

    assert await _alerts(session) == []
    assert any(e["event"] == "chain.reconcile.scan_failed" for e in logs)
    assert not any(e["event"].endswith("_missing") for e in logs)


async def test_missing_scan_block_skips_the_run(
    session, reconciliation, chain, lawyer, monkeypatch
):
    monkeypatch.setattr(settings, "CHAIN_SCAN_FROM_BLOCK", None)
    await chain.store_document_hash(str(uuid.uuid4()), "ab" * 32)

    with capture_logs() as logs:
        report = await reconciliation.run()

    assert report.skipped is True
    assert report.checked == 0
    assert await _alerts(session) == []
    assert [e["log_level"] for e in logs if e["event"] == "chain.reconcile.not_configured"] == [
        "error"
    ]


async def test_block_zero_is_a_configured_scan_start(reconciliation, monkeypatch):
    monkeypatch.setattr(settings, "CHAIN_SCAN_FROM_BLOCK", 0)

    assert (await reconciliation.run()).skipped is False


# === Admin ===


async def test_admin_route_returns_the_report(reconciliation, chain, lawyer):
    await chain.store_document_hash(str(uuid.uuid4()), "ab" * 32)

    report = await reconcile_chain(as_auth(lawyer), reconciliation)

    assert report.document_missing == 1


# === Providers ===


class FakeEvent:
    """Serves logs by block, recording each range the provider asks for."""

    def __init__(self, ids_by_block: dict[int, str], fail_at: int | None = None) -> None:
        self.ids_by_block = ids_by_block
        self.fail_at = fail_at
        self.ranges: list[tuple[int, int]] = []

    async def get_logs(self, from_block: int, to_block: int) -> list[dict]:
        self.ranges.append((from_block, to_block))
        if self.fail_at is not None and from_block <= self.fail_at <= to_block:
            raise ConnectionError("https://rpc.test/secret-key: range too large")
        return [
            {"args": {"documentId": document_id}}
            for block, document_id in self.ids_by_block.items()
            if from_block <= block <= to_block
        ]


def _provider(monkeypatch, event: FakeEvent, latest: int) -> Web3BlockchainProvider:
    async def block_number() -> int:
        return latest

    class Eth:
        block_number = property(lambda self: block_number())

    contract = SimpleNamespace(
        events=SimpleNamespace(DocumentRecordVerified=lambda: event)
    )
    monkeypatch.setattr(Web3BlockchainProvider, "_contract", contract)
    provider = Web3BlockchainProvider.__new__(Web3BlockchainProvider)
    provider.w3 = SimpleNamespace(eth=Eth())  # type: ignore[assignment]
    return provider


async def test_scan_covers_every_block_once_across_chunks(monkeypatch):
    monkeypatch.setattr(settings, "CHAIN_LOG_CHUNK_BLOCKS", 10)
    # Either side of each chunk boundary, and the first and last block.
    event = FakeEvent({100: "a", 109: "b", 110: "c", 119: "d", 120: "e", 125: "f"})

    ids = await _provider(monkeypatch, event, latest=125).list_anchored_document_ids(100)

    assert ids == {"a", "b", "c", "d", "e", "f"}
    assert event.ranges == [(100, 109), (110, 119), (120, 125)]


async def test_scan_ending_on_a_chunk_boundary_reads_no_empty_range(monkeypatch):
    monkeypatch.setattr(settings, "CHAIN_LOG_CHUNK_BLOCKS", 10)
    event = FakeEvent({})

    await _provider(monkeypatch, event, latest=119).list_anchored_document_ids(100)

    assert event.ranges == [(100, 109), (110, 119)]


async def test_a_failed_chunk_fails_the_whole_scan(monkeypatch):
    monkeypatch.setattr(settings, "CHAIN_LOG_CHUNK_BLOCKS", 10)
    event = FakeEvent({100: "a"}, fail_at=115)

    with pytest.raises(ServiceUnavailableError) as failure:
        await _provider(monkeypatch, event, latest=125).list_anchored_document_ids(100)

    # An RPC error can carry the provider URL, and with it the API key.
    assert "secret-key" not in str(failure.value)


async def test_a_start_past_the_latest_block_is_not_a_clean_scan(monkeypatch):
    event = FakeEvent({})

    with pytest.raises(ServiceUnavailableError):
        await _provider(monkeypatch, event, latest=99).list_anchored_document_ids(100)


async def test_mock_provider_has_no_anchors():
    assert await MockBlockchainProvider().list_anchored_document_ids(0) == set()
