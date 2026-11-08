"""Detecting and restoring tampered analysis: approved text, chunks and insight."""

import hashlib
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from app.core import settings
from app.features.blockchain.owner_watch import _describe
from app.features.blockchain.reconciliation import ChainReconciliation
from app.features.blockchain.service import BlockchainService
from app.features.chunks.models import DocumentChunk
from app.features.chunks.repository import DocumentChunkRepository
from app.features.documents.extraction.models import DocumentExtraction
from app.features.documents.models import Document
from app.features.documents.workflow.finalization import DocumentFinalization
from app.features.documents.workflow.restoration import DocumentRestoration
from app.features.documents.workflow.tamper_reports import TamperReports
from app.features.insights.models import DocumentInsight
from app.features.notifications.models import Notification, NotificationType
from app.features.snapshot.backups import SnapshotArchive
from app.features.snapshot.records import analysis_record, canonical_json
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.infrastructure.blockchain import OwnerEvent
from app.infrastructure.nlp.embeddings import EMBEDDING_DIM
from app.shared.errors.exceptions import ConflictError, ServiceUnavailableError
from tests.conftest import as_auth, mark_processed, pdf
from tests.features.retrieval.test_search import FixedEmbedder
from tests.features.snapshot.test_backups import FakeBackup

VECTOR = [1.0] * EMBEDDING_DIM
CHUNKS = ["The seller sells the lot.", "The buyer pays 1,000,000.", "Penalty: 10% a month."]


@pytest.fixture
def backup() -> FakeBackup:
    return FakeBackup()


@pytest.fixture
def archive(session, storage, chain, backup) -> SnapshotArchive:
    return SnapshotArchive(session, backup, chain)


@pytest.fixture
def verifier(session, storage, chain, backup) -> BlockchainService:
    return BlockchainService(session, chain, backup)


@pytest.fixture
def restoration(session, storage, chain, backup) -> DocumentRestoration:
    return DocumentRestoration(session, chain, backup)


@pytest.fixture(autouse=True)
def reembeds(monkeypatch) -> list[bool]:
    queued: list[bool] = []
    monkeypatch.setattr(
        "app.celery_tasks.documents.reembed_chunks_task.delay",
        lambda *a, **k: queued.append(True),
    )
    return queued


@pytest.fixture(autouse=True)
def no_alert_emails(monkeypatch) -> None:
    async def record(subject, body):
        pass

    monkeypatch.setattr("app.features.snapshot.backups.send_backup_alert", record)


async def _analyse(session, document_id, reviewer) -> None:
    """Stand in for OCR, review and enrichment."""
    extraction = DocumentExtraction(
        document_id=document_id,
        engine="mock",
        text="raw ocr",
        page_count=1,
        meta={},
        reviewed_text=" ".join(CHUNKS),
        reviewed_by=reviewer.id,
        reviewed_at=datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc),
    )
    session.add(extraction)
    await session.flush()
    for index, text in enumerate(CHUNKS):
        session.add(
            DocumentChunk(
                document_id=document_id,
                extraction_id=extraction.id,
                chunk_index=index,
                text=text,
                char_count=len(text),
                embedding_vec=VECTOR,
                strategy="recursive",
                engine="mock",
                engine_version="1",
            )
        )
    session.add(
        DocumentInsight(
            document_id=document_id,
            extraction_id=extraction.id,
            engine="mock",
            engine_version="1",
            summary="A sale of a lot.",
            labels={"items": ["sale"]},
            entities={"items": [{"name": "Juan", "type": "PERSON"}]},
            risk_flags={"items": [{"clause": "Penalty: 10% a month.", "level": "high"}]},
            confidence=0.87,
        )
    )
    await session.flush()


@pytest.fixture
async def finalized(session, intake, queue, chain, backup, storage, lawyer, open_book) -> Document:
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    await _analyse(session, doc.document_id, lawyer)
    await DocumentFinalization(session, chain, backup).finalize(me, doc.document_id)
    return await session.get(Document, doc.document_id)


async def _rows(session, document):
    """The live rows. `document` may be an ID, for after `expire_all`."""
    document_id = getattr(document, "id", document)
    extraction = (
        await session.execute(
            select(DocumentExtraction).where(DocumentExtraction.document_id == document_id)
        )
    ).scalar_one()
    chunks = list(
        (
            await session.execute(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document_id)
                .order_by(DocumentChunk.chunk_index)
            )
        ).scalars()
    )
    insight = (
        await session.execute(
            select(DocumentInsight).where(DocumentInsight.document_id == document_id)
        )
    ).scalar_one_or_none()
    return extraction, chunks, insight


def _analysis_objects(backup) -> dict[str, bytes]:
    return {k: v for k, v in backup.objects.items() if "/analysis-" in k}


async def _notifications(session) -> list[Notification]:
    result = await session.execute(
        select(Notification).where(Notification.type == NotificationType.RECORD_ALTERED)
    )
    return list(result.scalars().all())


# === Freeze and anchor ===


async def test_finalize_archives_and_anchors_the_analysis_record(
    session, finalized, chain, backup, lawyer
):
    extraction, chunks, insight = await _rows(session, finalized)
    expected = canonical_json(analysis_record(extraction, chunks, insight))
    expected_hash = hashlib.sha256(expected).hexdigest()
    snapshot = await DocumentSnaphotRepository(session).get_by_document_id(finalized.id)

    assert chain.records[f"{finalized.id}:analysis"].data_hash == expected_hash
    assert backup.objects[f"{finalized.id}/analysis-{expected_hash}.json"] == expected
    assert snapshot.analysis_sha256 == expected_hash
    record = json.loads(expected)
    assert record["approved_text"] == " ".join(CHUNKS)
    assert record["reviewed_by"] == str(lawyer.id)
    assert [c["text"] for c in record["chunks"]] == CHUNKS
    assert record["insight"]["risk_flags"] == insight.risk_flags
    assert "embedding" not in record["chunks"][0]


async def test_a_failed_analysis_archive_refuses_finalize(
    session, intake, queue, chain, backup, storage, lawyer, open_book, monkeypatch
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    await _analyse(session, doc.document_id, lawyer)
    real_store = backup.store

    async def no_analysis(key, data, content_type):
        if "/analysis-" in key:
            raise ConnectionError("bucket unreachable")
        await real_store(key, data, content_type)

    monkeypatch.setattr(backup, "store", no_analysis)

    with pytest.raises(ServiceUnavailableError):
        await DocumentFinalization(session, chain, backup).finalize(me, doc.document_id)
    assert chain.records == {}


async def test_failed_analysis_anchor_keeps_finalize_and_retry_uses_the_archive(
    session, intake, queue, chain, backup, storage, archive, lawyer, open_book, monkeypatch
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    await _analyse(session, doc.document_id, lawyer)
    real_store = chain.store_document_hash

    async def no_analysis(document_id, data_hash):
        if document_id.endswith(":analysis"):
            raise ConnectionError("rpc unreachable")
        return await real_store(document_id, data_hash)

    monkeypatch.setattr(chain, "store_document_hash", no_analysis)
    await DocumentFinalization(session, chain, backup).finalize(me, doc.document_id)
    monkeypatch.setattr(chain, "store_document_hash", real_store)
    document = await session.get(Document, doc.document_id)
    assert document.lifecycle == "FINALIZED"
    assert f"{document.id}:metadata" in chain.records
    assert f"{document.id}:analysis" not in chain.records
    [archived] = _analysis_objects(backup).values()
    _, _, insight = await _rows(session, document)
    insight.summary = "Edited in between"
    await session.flush()

    assert await archive.anchor_pending_metadata() == 1

    assert (
        chain.records[f"{document.id}:analysis"].data_hash
        == hashlib.sha256(archived).hexdigest()
    )
    assert await archive.anchor_pending_metadata() == 0


# === Detect ===


def _set_insight(field, value):
    async def edit(session, document):
        _, _, insight = await _rows(session, document)
        setattr(insight, field, value)

    return edit


async def _edit_chunk(session, document):
    _, chunks, _ = await _rows(session, document)
    chunks[2].text = "Penalty: none."


async def _edit_text(session, document):
    extraction, _, _ = await _rows(session, document)
    extraction.reviewed_text = "Something else entirely."


async def _delete_chunk(session, document):
    _, chunks, _ = await _rows(session, document)
    await session.delete(chunks[1])


async def _delete_insight(session, document):
    await session.execute(
        delete(DocumentInsight).where(DocumentInsight.document_id == document.id)
    )


ATTACKS = {
    "labels": (_set_insight("labels", {"items": ["lease"]}), "insight.labels"),
    "entities": (_set_insight("entities", {"items": []}), "insight.entities"),
    "risk_flags": (_set_insight("risk_flags", None), "insight.risk_flags"),
    "summary": (_set_insight("summary", "Harmless."), "insight.summary"),
    "chunk_text": (_edit_chunk, "chunks[2].text"),
    "approved_text": (_edit_text, "approved_text"),
    "deleted_chunk": (_delete_chunk, "chunks[1]"),
    "deleted_insight": (_delete_insight, "insight"),
}


@pytest.mark.parametrize("attack", ATTACKS)
async def test_each_attack_is_named_on_verify_in_the_job_and_in_tamper_reports(
    attack, session, finalized, verifier, archive, restoration, lawyer
):
    edit, field = ATTACKS[attack]
    await edit(session, finalized)
    await session.flush()

    result = await verifier.verify_document(as_auth(lawyer), finalized.id)
    report = await archive.verify()
    await archive.verify()

    assert result.status == "AUTHENTIC"
    assert result.metadata_status == "MATCH"
    assert result.analysis_status == "ALTERED"
    assert [c.field for c in result.analysis_changes] == [field]
    assert result.needs_restore is True
    assert [(p.kind, p.field) for p in report.records.problems] == [("analysis", field)]
    # The metadata record is still counted as fine.
    assert (report.records.altered, report.records.ok) == (1, 1)
    [alert] = await _notifications(session)
    assert alert.user_id == lawyer.id
    reports = await TamperReports(restoration).list_open(as_auth(lawyer))
    [item] = reports.reports
    assert [(c.kind, c.field) for c in item.changes] == [("analysis", field)]
    assert item.restorable is True


async def test_long_text_is_shown_as_an_excerpt(session, finalized, verifier, lawyer):
    extraction, _, _ = await _rows(session, finalized)
    extraction.reviewed_text = "x" * 5000
    await session.flush()

    result = await verifier.verify_document(as_auth(lawyer), finalized.id)

    [change] = result.analysis_changes
    assert len(str(change.current)) < 200
    assert len(str(change.anchored)) < 200


async def test_an_edited_embedding_raises_nothing(session, finalized, verifier, archive, lawyer):
    _, chunks, _ = await _rows(session, finalized)
    chunks[0].embedding_vec = [0.5] * EMBEDDING_DIM
    await session.flush()

    result = await verifier.verify_document(as_auth(lawyer), finalized.id)
    report = await archive.verify()

    assert result.analysis_status == "MATCH"
    assert result.needs_restore is False
    assert report.records.problems == []


async def test_no_analysis_anchor_is_not_anchored(session, finalized, chain, verifier, archive, lawyer):
    del chain.records[f"{finalized.id}:analysis"]
    await _edit_chunk(session, finalized)
    await session.flush()

    result = await verifier.verify_document(as_auth(lawyer), finalized.id)
    report = await archive.verify()

    assert result.analysis_status == "NOT_ANCHORED"
    assert result.needs_restore is False
    assert (report.records.not_anchored, report.records.ok) == (1, 1)
    assert report.records.problems == []


async def test_a_storage_outage_is_unknown_with_no_finding(
    session, finalized, backup, verifier, archive, lawyer, monkeypatch
):
    await _edit_chunk(session, finalized)
    await session.flush()

    async def down(_key):
        raise ConnectionError("bucket unreachable")

    monkeypatch.setattr(backup, "exists", down)

    result = await verifier.verify_document(as_auth(lawyer), finalized.id)
    report = await archive.verify()

    assert result.analysis_status == "UNAVAILABLE"
    assert report.records.problems == []
    assert await _notifications(session) == []


async def test_a_chain_outage_is_unknown_with_no_finding(
    session, finalized, chain, verifier, archive, lawyer, monkeypatch
):
    await _edit_chunk(session, finalized)
    await session.flush()
    real_read = chain.get_document_hash

    async def no_analysis(document_id, *a, **k):
        if document_id.endswith(":analysis"):
            raise ServiceUnavailableError("rpc unreachable")
        return await real_read(document_id, *a, **k)

    monkeypatch.setattr(chain, "get_document_hash", no_analysis)

    result = await verifier.verify_document(as_auth(lawyer), finalized.id)
    report = await archive.verify()

    assert result.analysis_status == "UNAVAILABLE"
    assert report.records.problems == []
    assert await _notifications(session) == []


async def test_reconcile_skips_analysis_anchors(session, finalized, chain, monkeypatch):
    monkeypatch.setattr(settings, "CHAIN_SCAN_FROM_BLOCK", 100)

    report = await ChainReconciliation(session, chain).run()

    assert (report.checked, report.ok, report.problems) == (1, 1, [])


def test_the_owner_watch_names_an_analysis_record():
    event = OwnerEvent(
        kind="correction",
        tx_hash="0x" + "11" * 32,
        block_number=1,
        document_id="abc:analysis",
        previous_hash="aa",
        new_hash="bb",
        reason="fix",
    )

    _, body = _describe(event)

    assert "analysis record anchor of document abc " in body


# === Restore ===


async def test_one_restore_brings_back_text_chunks_and_insight(
    session, finalized, verifier, restoration, queries, lawyer, reembeds
):
    extraction, chunks, insight = await _rows(session, finalized)
    before = analysis_record(extraction, chunks, insight)
    extraction.reviewed_text = "Forged"
    chunks[2].text = "Penalty: none."
    await session.delete(chunks[1])
    insight.risk_flags = None
    session.add(
        DocumentChunk(
            document_id=finalized.id,
            extraction_id=extraction.id,
            chunk_index=3,
            text="Planted.",
            char_count=8,
            strategy="recursive",
            engine="mock",
        )
    )
    await session.flush()

    result = await restoration.restore(as_auth(lawyer), finalized.id)

    assert result.restored is True
    assert [(r.record, r.field) for r in result.repaired] == [
        ("analysis", "approved_text"),
        ("analysis", "chunks[1]"),
        ("analysis", "chunks[2].text"),
        ("analysis", "chunks[3]"),
        ("analysis", "insight.risk_flags"),
    ]
    document_id, me = finalized.id, as_auth(lawyer)
    session.expire_all()
    extraction, chunks, insight = await _rows(session, document_id)
    assert analysis_record(extraction, chunks, insight) == before
    assert [c.embedding_vec is None for c in chunks] == [False, True, True]
    assert reembeds == [True]
    after = await verifier.verify_document(me, document_id)
    assert after.analysis_status == "MATCH"
    assert after.needs_restore is False
    last = (await queries.get_history(me, document_id)).changes[-1]
    assert ("analysis", "chunks[2].text") in [(c.record, c.field) for c in last.changes]


async def test_a_deleted_insight_comes_back(session, finalized, restoration, lawyer):
    extraction, chunks, insight = await _rows(session, finalized)
    before = analysis_record(extraction, chunks, insight)
    await _delete_insight(session, finalized)
    await session.flush()

    await restoration.restore(as_auth(lawyer), finalized.id)

    document_id = finalized.id
    session.expire_all()
    assert analysis_record(*await _rows(session, document_id)) == before


async def test_restored_chunks_are_searchable_after_reembedding(
    session, finalized, restoration, lawyer
):
    from app.workers.reembed_chunks import reembed_document

    await _edit_chunk(session, finalized)
    await session.flush()
    await restoration.restore(as_auth(lawyer), finalized.id)
    document_id = finalized.id
    session.expire_all()
    _, chunks, _ = await _rows(session, document_id)
    vector = (await FixedEmbedder().embed("x")).vector
    for chunk in chunks:
        if chunk.embedding_vec is None:
            await DocumentChunkRepository(session).set_embedding(chunk.id, vector, None)
    await session.flush()

    hits = await DocumentChunkRepository(session).search_document(document_id, vector, 10)

    assert "Penalty: 10% a month." in [chunk.text for chunk, _ in hits]
    assert "Penalty: none." not in [chunk.text for chunk, _ in hits]
    assert reembed_document  # the job that fills the vectors in production


async def test_restore_refuses_an_archived_json_that_does_not_match_the_anchor(
    session, finalized, backup, restoration, lawyer
):
    [key] = _analysis_objects(backup)
    backup.objects[key] = canonical_json({"approved_text": "Forged", "version": 1})
    await _edit_chunk(session, finalized)
    await session.flush()

    with pytest.raises(ConflictError) as refused:
        await restoration.restore(as_auth(lawyer), finalized.id)

    assert refused.value.details["reason"] == "BACKUP_MISMATCH"
    _, chunks, _ = await _rows(session, finalized)
    assert chunks[2].text == "Penalty: none."


async def test_restore_with_a_storage_outage_writes_nothing(
    session, finalized, backup, restoration, lawyer, monkeypatch
):
    await _edit_chunk(session, finalized)
    await session.flush()

    async def down(_key):
        raise ConnectionError("bucket unreachable")

    monkeypatch.setattr(backup, "exists", down)

    with pytest.raises(ServiceUnavailableError):
        await restoration.restore(as_auth(lawyer), finalized.id)
