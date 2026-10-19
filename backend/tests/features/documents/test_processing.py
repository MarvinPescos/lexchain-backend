"""The processing pipeline: durable dispatch, retries, and safe re-delivery."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.chunks.models import DocumentChunk
from app.features.documents.constants import (
    JOB_ENRICH,
    JOB_EXTRACT,
    STATUS_AWAITING_REVIEW,
    STATUS_COMPLETED,
    STATUS_ENRICHING,
    STATUS_FAILED,
    STATUS_QUEUED,
)
from app.features.documents.dispatch import DocumentDispatcher
from app.features.documents.extraction.models import DocumentExtraction
from app.features.documents.extraction.service import ExtractionReviewService
from app.features.documents.models import Document, ProcessingDispatch
from app.features.insights.models import DocumentInsight
from app.infrastructure.nlp.analysis.base import AnalysisResult
from app.infrastructure.nlp.chunking.base import Chunk, ChunkResult
from app.infrastructure.nlp.embeddings.base import EmbeddingResult
from app.infrastructure.nlp.ocr.base import ExtractionResult
from app.workers import dispatch_pending as relay
from app.workers import process_documents as worker
from tests.conftest import RecordingQueue, as_auth, pdf

# === Fakes ===


class BrokerDown(RecordingQueue):
    async def enqueue_process_document(self, document_id, **kwargs) -> None:
        raise ConnectionError("broker unreachable")

    async def enqueue_enrich_document(self, document_id) -> None:
        raise ConnectionError("broker unreachable")


class RecordingEnrichQueue(RecordingQueue):
    def __init__(self) -> None:
        super().__init__()
        self.enriched: list = []

    async def enqueue_enrich_document(self, document_id) -> None:
        self.enriched.append(document_id)


class Flaky:
    """Raises the queued errors in order, then answers."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    def check(self) -> None:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)


class FakeOCR(Flaky):
    async def extract(self, file_data: bytes) -> ExtractionResult:
        self.check()
        text = "First paragraph.\n\nSecond paragraph."
        return ExtractionResult(
            text=text, ingestion_text=text, page_count=1, meta={"engine": "fake"}
        )


class FakeChunker:
    async def split(self, text: str) -> ChunkResult:
        parts = [p for p in text.split("\n\n") if p]
        return ChunkResult(
            chunks=[Chunk(index=i, text=p, char_count=len(p)) for i, p in enumerate(parts)],
            strategy="fake",
        )


class FakeEmbedder(Flaky):
    async def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]:
        self.check()
        return [EmbeddingResult(vector=[0.1] * 384, dim=384) for _ in texts]


class FakeAnalyzer:
    async def analyze(self, text: str) -> AnalysisResult:
        return AnalysisResult(summary="A deed.", labels=["deed"], meta={"engine": "fake"})


def http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.test")
    return httpx.HTTPStatusError(
        "provider", request=request, response=httpx.Response(status, request=request)
    )


# === Fixtures ===


@pytest.fixture
def worker_db(monkeypatch: pytest.MonkeyPatch, session: AsyncSession, storage) -> None:
    """Point the workers at the test transaction and the fake storage."""

    def local_session() -> AsyncSession:
        return AsyncSession(
            bind=session.bind,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )

    for module in (worker, relay):
        monkeypatch.setattr(module, "AsyncSessionLocal", local_session)
    monkeypatch.setattr(worker, "get_storage_client", lambda: storage)


@pytest.fixture
def notified(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    statuses: list[str] = []
    monkeypatch.setattr(
        worker.handle_document_processed,
        "delay",
        lambda **kw: statuses.append(kw["status"]),
    )
    return statuses


@pytest.fixture
def ocr(monkeypatch: pytest.MonkeyPatch) -> FakeOCR:
    fake = FakeOCR()
    monkeypatch.setattr(worker, "build_ocr", lambda file_data: fake)
    return fake


@pytest.fixture
async def finalized(intake, finalization, lawyer, open_book):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await finalization.finalize(me, doc.document_id)
    return doc.document_id


async def _status(session: AsyncSession, document_id) -> str | None:
    return await session.scalar(select(Document.status).where(Document.id == document_id))


async def _count(session: AsyncSession, model, document_id) -> int:
    return await session.scalar(
        select(func.count()).select_from(model).where(model.document_id == document_id)
    )


async def _dispatches(session: AsyncSession) -> list[ProcessingDispatch]:
    result = await session.execute(select(ProcessingDispatch))
    return list(result.scalars().all())


async def _extract(session, document_id, final_attempt=True) -> None:
    document = await session.get(Document, document_id)
    await session.refresh(document, ["signed_copy"])
    await worker.extract_document_job(
        document_id, document.signed_copy.storage_url, final_attempt=final_attempt
    )


async def _enrich(document_id, embedder, final_attempt=True) -> None:
    await worker.enrich_document_job(
        document_id,
        analyzer=FakeAnalyzer(),
        chunker=FakeChunker(),
        embedder=embedder,
        final_attempt=final_attempt,
    )


async def _approve(session, lawyer, document_id, queue) -> None:
    await ExtractionReviewService(session, queue).approve(as_auth(lawyer), document_id)


# === Durable dispatch (#9) ===


async def test_finalize_sends_and_marks_the_dispatch(session, finalized, queue):
    assert queue.processed == [finalized]
    [dispatch] = await _dispatches(session)
    assert (dispatch.job, dispatch.attempts) == (JOB_EXTRACT, 0)
    assert dispatch.dispatched_at is not None


async def test_broker_down_at_finalize_leaves_the_job_pending(
    session, intake, storage, chain, lawyer, open_book
):
    from app.features.documents.workflow.finalization import DocumentFinalization

    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    record = await DocumentFinalization(session, BrokerDown(), chain).finalize(
        me, doc.document_id
    )

    assert record.tx_hash
    assert await _status(session, doc.document_id) == STATUS_QUEUED
    [dispatch] = await _dispatches(session)
    assert dispatch.dispatched_at is None
    assert dispatch.attempts == 1
    assert "broker unreachable" in dispatch.last_error

    # The broker is back: the pending job goes out once.
    queue = RecordingQueue()
    assert await DocumentDispatcher(session, queue).send(dispatch) is True
    assert queue.processed == [doc.document_id]
    assert dispatch.dispatched_at is not None


async def test_broker_down_at_approval_leaves_the_job_pending(
    session, worker_db, ocr, notified, finalized, lawyer
):
    await _extract(session, finalized)
    await _approve(session, lawyer, finalized, BrokerDown())

    assert await _status(session, finalized) == STATUS_ENRICHING
    pending = [d for d in await _dispatches(session) if d.job == JOB_ENRICH]
    assert [d.dispatched_at for d in pending] == [None]

    queue = RecordingEnrichQueue()
    assert await DocumentDispatcher(session, queue).send(pending[0]) is True
    assert queue.enriched == [finalized]


async def test_relay_resends_only_stale_pending_dispatches(
    session, worker_db, monkeypatch, intake, chain, lawyer, open_book
):
    from app.features.documents.workflow.finalization import DocumentFinalization

    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await DocumentFinalization(session, BrokerDown(), chain).finalize(me, doc.document_id)
    queue = RecordingQueue()
    monkeypatch.setattr(relay, "get_document_queue", lambda: queue)

    # Too fresh: still the recording request's to send.
    assert await relay.dispatch_pending() == 0

    [dispatch] = await _dispatches(session)
    dispatch.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await session.flush()
    assert await relay.dispatch_pending() == 1
    assert queue.processed == [doc.document_id]

    # Delivered: never sent again.
    assert await relay.dispatch_pending() == 0
    assert queue.processed == [doc.document_id]


# === Retries (#10) ===


async def test_transient_ocr_error_retries_then_succeeds(
    session, worker_db, ocr, notified, finalized
):
    ocr.errors = [httpx.ConnectError("ocr down")]

    with pytest.raises(httpx.ConnectError):
        await _extract(session, finalized, final_attempt=False)
    assert await _status(session, finalized) != STATUS_FAILED
    assert notified == []

    await _extract(session, finalized, final_attempt=False)
    assert await _status(session, finalized) == STATUS_AWAITING_REVIEW
    assert await _count(session, DocumentExtraction, finalized) == 1
    assert notified == ["awaiting_review"]


async def test_last_attempt_marks_failed_and_notifies_once(
    session, worker_db, ocr, notified, finalized
):
    ocr.errors = [httpx.ConnectError("ocr down")]

    await _extract(session, finalized, final_attempt=True)
    assert await _status(session, finalized) == STATUS_FAILED
    assert notified == ["failed"]


async def test_permanent_error_fails_without_retrying(
    session, worker_db, ocr, notified, finalized
):
    ocr.errors = [http_error(400)]

    await _extract(session, finalized, final_attempt=False)
    assert await _status(session, finalized) == STATUS_FAILED
    assert notified == ["failed"]


@pytest.mark.parametrize("status", [429, 503])
def test_rate_limits_and_server_errors_are_retryable(status):
    assert worker.is_retryable(http_error(status))
    assert not worker.is_retryable(http_error(404))


async def test_extract_delivered_twice_does_nothing_the_second_time(
    session, worker_db, ocr, notified, finalized
):
    await _extract(session, finalized)
    await _extract(session, finalized)

    assert ocr.calls == 1
    assert await _count(session, DocumentExtraction, finalized) == 1
    assert notified == ["awaiting_review"]


async def test_enrichment_retry_rebuilds_partial_output(
    session, worker_db, ocr, notified, finalized, lawyer
):
    await _extract(session, finalized)
    await _approve(session, lawyer, finalized, RecordingEnrichQueue())

    # Chunks are committed before embedding fails.
    embedder = FakeEmbedder(httpx.ReadTimeout("embedder slow"))
    with pytest.raises(httpx.ReadTimeout):
        await _enrich(finalized, embedder, final_attempt=False)
    assert await _count(session, DocumentChunk, finalized) == 2
    assert await _status(session, finalized) == STATUS_ENRICHING

    await _enrich(finalized, embedder, final_attempt=False)
    assert await _status(session, finalized) == STATUS_COMPLETED
    assert await _count(session, DocumentChunk, finalized) == 2
    assert await _count(session, DocumentInsight, finalized) == 1
    assert notified == ["awaiting_review", "completed"]

    # Delivered again after completing: nothing changes.
    await _enrich(finalized, FakeEmbedder())
    assert await _count(session, DocumentChunk, finalized) == 2
    assert await _count(session, DocumentInsight, finalized) == 1
    assert notified == ["awaiting_review", "completed"]
