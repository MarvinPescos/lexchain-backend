"""Moving documents onto the current contract, only when every hash agrees."""

import pytest

from app.features.blockchain.repository import OnChainRecordRepository
from app.features.documents.models import Document
from app.features.documents.workflow.finalization import DocumentFinalization
from app.features.snapshot.records import canonical_json, metadata_record, sha256_hex
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.workers.reanchor_documents import reanchor
from tests.conftest import FakeChain, as_auth, mark_processed, pdf
from tests.features.snapshot.test_backups import FakeBackup


@pytest.fixture
def backup() -> FakeBackup:
    return FakeBackup()


@pytest.fixture
def new_chain() -> FakeChain:
    return FakeChain()


@pytest.fixture(autouse=True)
def live_storage(monkeypatch, storage):
    monkeypatch.setattr("app.workers.reanchor_documents.get_storage_client", lambda: storage)


@pytest.fixture
async def document(session, intake, queue, chain, storage, lawyer, open_book) -> Document:
    """Finalized on the old contract, with no metadata anchor."""
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    await DocumentFinalization(session, chain).finalize(me, doc.document_id)
    return await session.get(Document, doc.document_id)


async def test_dry_run_writes_nothing(session, document, chain, new_chain, backup):
    report = await reanchor(session, chain, new_chain, backup)

    assert report.ready == [str(document.id)]
    assert report.done == []
    assert new_chain.records == {}
    assert backup.objects == {}


async def test_apply_anchors_the_pdf_and_the_metadata(
    session, document, chain, new_chain, backup
):
    snapshot = await DocumentSnaphotRepository(session).get_by_document_id(document.id)
    new_chain_tx = "0x" + "00" * 32

    report = await reanchor(session, chain, new_chain, backup, apply=True)

    record = canonical_json(metadata_record(document))
    assert report.done == [str(document.id)]
    assert new_chain.records[str(document.id)].data_hash == snapshot.file_sha256
    assert new_chain.records[f"{document.id}:metadata"].data_hash == sha256_hex(record)
    assert backup.objects[f"{document.id}/metadata-{sha256_hex(record)}.json"] == record
    assert snapshot.metadata_sha256 == sha256_hex(record)
    anchored = await OnChainRecordRepository(session).get_by_document_id(document.id)
    assert anchored.tx_hash == new_chain_tx


async def test_a_hash_overwritten_on_the_old_contract_goes_to_review(
    session, document, chain, new_chain, backup
):
    await chain.store_document_hash(str(document.id), "ab" * 32)

    report = await reanchor(session, chain, new_chain, backup, apply=True)

    assert report.done == []
    assert "old contract" in report.review[str(document.id)]
    assert new_chain.records == {}


async def test_a_changed_live_file_goes_to_review(
    session, document, storage, chain, new_chain, backup
):
    [url] = storage.objects
    storage.objects[url] += b" tampered"

    report = await reanchor(session, chain, new_chain, backup, apply=True)

    assert "live file" in report.review[str(document.id)]
    assert new_chain.records == {}


async def test_an_altered_snapshot_goes_to_review(session, document, chain, new_chain, backup):
    snapshot = await DocumentSnaphotRepository(session).get_by_document_id(document.id)
    snapshot.file_sha256 = "0" * 64
    await session.flush()

    report = await reanchor(session, chain, new_chain, backup, apply=True)

    assert str(document.id) in report.review
    assert new_chain.records == {}


async def test_one_failure_goes_to_review_and_the_run_carries_on(
    session, intake, queue, document, chain, new_chain, backup, lawyer, open_book, monkeypatch
):
    me = as_auth(lawyer)
    second = await intake.upload_signed(me, pdf("second"), "Second", open_book.id)
    await mark_processed(session, second.document_id)
    await DocumentFinalization(session, chain).finalize(me, second.document_id)
    real_store = new_chain.store_document_hash
    failing_id = str(document.id)  # the rollback expires `document`

    async def first_fails(document_id, data_hash):
        if document_id == failing_id:
            raise ConnectionError("rpc unreachable")
        return await real_store(document_id, data_hash)

    monkeypatch.setattr(new_chain, "store_document_hash", first_fails)

    report = await reanchor(session, chain, new_chain, backup, apply=True)

    assert report.done == [str(second.document_id)]
    assert "failed" in report.review[failing_id]


async def test_a_document_already_moved_is_not_anchored_twice(
    session, document, chain, new_chain, backup
):
    await reanchor(session, chain, new_chain, backup, apply=True)
    before = dict(new_chain.records)

    report = await reanchor(session, chain, new_chain, backup, apply=True)

    assert report.done == []
    assert new_chain.records == before
