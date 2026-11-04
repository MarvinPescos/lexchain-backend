"""Risk clauses before finalize — issue #43.

The signed copy is processed while the document is SIGNED, so the lawyer sees
its risk clauses while it can still be replaced. Finalize waits for that, and
a job for a replaced copy never writes over the new one.
"""

import hashlib

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.chunks.models import DocumentChunk
from app.features.documents.constants import (
    JOB_EXTRACT,
    STATUS_AWAITING_REVIEW,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
)
from app.features.documents.extraction.models import DocumentExtraction
from app.features.documents.extraction.service import ExtractionReviewService
from app.features.documents.models import Document, ProcessingDispatch
from app.features.insights.models import DocumentInsight
from app.infrastructure.nlp.analysis import llm_analysis
from app.infrastructure.nlp.analysis.base import (
    ANALYSIS_STATUS_COMPLETE,
    ANALYSIS_STATUS_PARTIAL,
    AnalysisFailed,
    AnalysisResult,
)
from app.infrastructure.nlp.llm.base import LLMResult
from app.infrastructure.nlp.ocr.base import ExtractionResult
from app.shared.errors.exceptions import ConflictError
from app.workers import process_documents as worker
from tests.conftest import RecordingQueue, as_auth, connect_google, pdf
from tests.features.documents.test_processing import (
    FakeChunker,
    FakeEmbedder,
    http_error,
    notified,  # noqa: F401 — fixture
    worker_db,  # noqa: F401 — fixture
)

RISK = {"clause": "Seller may cancel at will", "severity": "high"}


# === Fakes ===


class EchoOCR:
    """Reads the file's own bytes as its text, so results show which copy they came from.

    `during` runs inside the OCR call — to replace the copy mid-job.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.during = None
        self.error: Exception | None = None

    async def extract(self, file_data: bytes) -> ExtractionResult:
        self.calls += 1
        if self.during:
            during, self.during = self.during, None
            await during()
        if self.error:
            raise self.error
        text = file_data.decode(errors="ignore")
        return ExtractionResult(
            text=text, ingestion_text=text, page_count=1, meta={"engine": "echo"}
        )


class RiskAnalyzer:
    """Flags RISK and remembers the text it saw; `during` runs inside the call."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.during = None

    async def analyze(self, text: str) -> AnalysisResult:
        self.seen.append(text)
        if self.during:
            during, self.during = self.during, None
            await during()
        return AnalysisResult(
            summary="A deed.",
            risk_flags=[RISK],
            meta={"engine": "fake", "analysis_status": ANALYSIS_STATUS_COMPLETE},
        )


class FailingAnalyzer:
    async def analyze(self, text: str) -> AnalysisResult:
        raise AnalysisFailed("unreadable")


class ScriptedLLM:
    """Answers each call with the next scripted reply; records what it was sent."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.sent: list[str] = []

    async def generate(self, messages, temperature=0.2, max_tokens=512) -> LLMResult:
        self.sent.append(messages[-1].content)
        return LLMResult(answer=self.answers.pop(0), model="scripted")


# === Fixtures and helpers ===


@pytest.fixture
def ocr(monkeypatch: pytest.MonkeyPatch) -> EchoOCR:
    fake = EchoOCR()
    monkeypatch.setattr(worker, "build_ocr", lambda file_data: fake)
    return fake


@pytest.fixture
def analyzer() -> RiskAnalyzer:
    return RiskAnalyzer()


async def _doc(session: AsyncSession, document_id) -> Document:
    document = await session.get(Document, document_id)
    await session.refresh(document)
    return document


async def _count(session: AsyncSession, model, document_id) -> int:
    return await session.scalar(
        select(func.count()).select_from(model).where(model.document_id == document_id)
    )


async def _extract(session: AsyncSession, document_id, copy_id=None) -> None:
    document = await _doc(session, document_id)
    await session.refresh(document, ["signed_copy"])
    await worker.extract_document_job(
        document_id,
        document.signed_copy.storage_url,
        signed_copy_id=copy_id or document.signed_copy_id,
    )


async def _enrich(document_id, analyzer, copy_id=None) -> None:
    await worker.enrich_document_job(
        document_id,
        analyzer=analyzer,
        chunker=FakeChunker(),
        embedder=FakeEmbedder(),
        signed_copy_id=copy_id,
    )


async def _process(session, lawyer, document_id, analyzer) -> None:
    """OCR → the lawyer approves the text → enrich, as the queue would run them."""
    await _extract(session, document_id)
    await ExtractionReviewService(session, RecordingQueue()).approve(
        as_auth(lawyer), document_id
    )
    await _enrich(document_id, analyzer)


# === Both paths process at SIGNED ===


async def test_path2_upload_shows_risks_while_signed(
    session, worker_db, ocr, analyzer, notified, intake, queries, queue, lawyer, open_book
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    assert (doc.lifecycle, doc.status) == ("SIGNED", STATUS_QUEUED)
    assert queue.copies == [doc.signed_copy.id]

    await _process(session, lawyer, doc.document_id, analyzer)

    doc = await queries.get_document(me, doc.document_id)
    assert (doc.lifecycle, doc.status) == ("SIGNED", STATUS_COMPLETED)
    assert doc.risk_flags == [RISK]
    assert doc.analysis_status == ANALYSIS_STATUS_COMPLETE
    assert doc.permissions.can_finalize


async def test_path1_attach_shows_risks_while_signed(
    session, worker_db, ocr, analyzer, notified, intake, drafting, google, queries,
    queue, lawyer, open_book,
):
    me = as_auth(lawyer)
    await connect_google(google, lawyer)
    doc = await intake.open_document(me, "Deed")
    await drafting.create_draft(me, doc.document_id)
    await drafting.mark_ready(me, doc.document_id)
    doc = await intake.attach_signed_copy(me, doc.document_id, pdf("deed"), open_book.id)
    assert (doc.lifecycle, doc.status) == ("SIGNED", STATUS_QUEUED)
    assert queue.processed == [doc.document_id]

    await _process(session, lawyer, doc.document_id, analyzer)

    doc = await queries.get_document(me, doc.document_id)
    assert (doc.lifecycle, doc.status, doc.risk_flags) == ("SIGNED", STATUS_COMPLETED, [RISK])


async def test_broker_down_at_attach_leaves_the_job_pending(
    session, storage, drafting, google, lawyer, open_book
):
    from app.features.documents.dispatch import DocumentDispatcher
    from app.features.documents.workflow.intake import DocumentIntake
    from tests.features.documents.test_processing import BrokerDown

    me = as_auth(lawyer)
    await connect_google(google, lawyer)
    intake = DocumentIntake(session, BrokerDown())
    doc = await intake.open_document(me, "Deed")
    await drafting.create_draft(me, doc.document_id)
    await drafting.mark_ready(me, doc.document_id)
    doc = await intake.attach_signed_copy(me, doc.document_id, pdf("deed"), open_book.id)

    assert (doc.lifecycle, doc.status) == ("SIGNED", STATUS_QUEUED)
    [dispatch] = (await session.execute(select(ProcessingDispatch))).scalars().all()
    assert (dispatch.job, dispatch.dispatched_at, dispatch.attempts) == (JOB_EXTRACT, None, 1)

    queue = RecordingQueue()
    assert await DocumentDispatcher(session, queue).send(dispatch) is True
    assert queue.copies == [doc.signed_copy.id]


# === The finalize gate ===


@pytest.mark.parametrize(
    "status", [STATUS_QUEUED, STATUS_AWAITING_REVIEW, STATUS_FAILED, None]
)
async def test_finalize_is_refused_until_processing_completes(
    status, session, intake, finalization, queries, lawyer, open_book
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    (await _doc(session, doc.document_id)).status = status
    await session.flush()

    assert not (await queries.get_document(me, doc.document_id)).permissions.can_finalize
    with pytest.raises(ConflictError) as refused:
        await finalization.finalize(me, doc.document_id)
    assert refused.value.details == {
        "reason": "PROCESSING_NOT_COMPLETE",
        "status": status,
    }
    assert (await _doc(session, doc.document_id)).lifecycle == "SIGNED"


async def test_finalize_anchors_the_current_copy_and_queues_nothing(
    session, worker_db, ocr, analyzer, notified, intake, finalization, queue, chain,
    lawyer, open_book, storage,
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await _process(session, lawyer, doc.document_id, analyzer)
    dispatches = await _count(session, ProcessingDispatch, doc.document_id)

    record = await finalization.finalize(me, doc.document_id)

    [stored] = storage.objects.values()
    assert record.data_hash == hashlib.sha256(stored).hexdigest()
    assert await _count(session, ProcessingDispatch, doc.document_id) == dispatches
    assert queue.processed == [doc.document_id]
    document = await _doc(session, doc.document_id)
    assert (document.lifecycle, document.status) == ("FINALIZED", STATUS_COMPLETED)


async def test_a_document_finalized_mid_pipeline_can_still_be_reviewed(
    session, worker_db, ocr, notified, intake, lawyer, open_book
):
    """Finalized under the old flow, before its OCR text was approved."""
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await _extract(session, doc.document_id)
    document = await _doc(session, doc.document_id)
    document.lifecycle = "FINALIZED"
    await session.flush()

    queue = RecordingQueue()
    await ExtractionReviewService(session, queue).approve(me, doc.document_id)
    assert (await _doc(session, doc.document_id)).status == "ENRICHING"


async def test_a_dispatch_dropped_by_a_replace_is_not_sent(
    session, storage, lawyer, open_book
):
    from app.features.documents.dispatch import DocumentDispatcher
    from app.features.documents.workflow.intake import DocumentIntake
    from tests.features.documents.test_processing import BrokerDown

    me = as_auth(lawyer)
    doc = await DocumentIntake(session, BrokerDown()).upload_signed(
        me, pdf("wrong-scan"), "Deed", open_book.id
    )
    [stale] = (await session.execute(select(ProcessingDispatch))).scalars().all()
    queue = RecordingQueue()
    await DocumentIntake(session, queue).replace_signed_copy(
        me, doc.document_id, pdf("right-scan"), "Wrong scan"
    )

    # The first request's send runs late: nothing more goes out.
    assert await DocumentDispatcher(session, queue).send(stale) is False
    assert len(queue.processed) == 1


# === Replacing the signed copy ===


async def test_replace_clears_old_results_and_processes_the_new_file(
    session, worker_db, ocr, analyzer, notified, intake, queries, queue, lawyer, open_book
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("wrong-scan"), "Deed", open_book.id)
    await _process(session, lawyer, doc.document_id, analyzer)

    replaced = await intake.replace_signed_copy(
        me, doc.document_id, pdf("right-scan"), reason="Clause needs re-signing"
    )
    assert replaced.status == STATUS_QUEUED
    assert (replaced.risk_flags, replaced.summary) == ([], None)
    assert not replaced.permissions.can_finalize
    for model in (DocumentExtraction, DocumentChunk, DocumentInsight):
        assert await _count(session, model, doc.document_id) == 0
    assert queue.copies == [doc.signed_copy.id, replaced.signed_copy.id]

    copies = await queries.get_signed_copies(me, doc.document_id)
    assert [c.is_current for c in copies.copies] == [True, False]

    await _process(session, lawyer, doc.document_id, analyzer)
    assert "right-scan" in analyzer.seen[-1]
    assert "wrong-scan" not in analyzer.seen[-1]
    assert (await _doc(session, doc.document_id)).status == STATUS_COMPLETED


async def test_replace_drops_jobs_not_yet_sent(
    session, storage, lawyer, open_book
):
    from app.features.documents.workflow.intake import DocumentIntake
    from tests.features.documents.test_processing import BrokerDown

    me = as_auth(lawyer)
    intake = DocumentIntake(session, BrokerDown())
    doc = await intake.upload_signed(me, pdf("wrong-scan"), "Deed", open_book.id)
    await intake.replace_signed_copy(me, doc.document_id, pdf("right-scan"), "Wrong scan")

    # Only the new copy's job is still owed.
    assert await _count(session, ProcessingDispatch, doc.document_id) == 1


async def test_ocr_job_for_a_replaced_copy_does_nothing(
    session, worker_db, ocr, notified, intake, lawyer, open_book
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("wrong-scan"), "Deed", open_book.id)
    old_copy = doc.signed_copy.id
    await intake.replace_signed_copy(me, doc.document_id, pdf("right-scan"), "Wrong scan")

    # The old copy's job is delivered late.
    await _extract(session, doc.document_id, copy_id=old_copy)

    assert ocr.calls == 0
    assert await _count(session, DocumentExtraction, doc.document_id) == 0
    assert (await _doc(session, doc.document_id)).status == STATUS_QUEUED


async def test_replace_during_ocr_wins_over_the_running_job(
    session, worker_db, ocr, notified, intake, lawyer, open_book
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("wrong-scan"), "Deed", open_book.id)
    ocr.during = lambda: intake.replace_signed_copy(
        me, doc.document_id, pdf("right-scan"), "Wrong scan"
    )

    await _extract(session, doc.document_id)

    assert await _count(session, DocumentExtraction, doc.document_id) == 0
    assert (await _doc(session, doc.document_id)).status == STATUS_QUEUED
    assert notified == []

    await _extract(session, doc.document_id)
    extraction = await session.scalar(
        select(DocumentExtraction).where(DocumentExtraction.document_id == doc.document_id)
    )
    assert "right-scan" in extraction.text


async def test_failure_of_a_replaced_copy_does_not_fail_the_new_one(
    session, worker_db, ocr, notified, intake, lawyer, open_book
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("wrong-scan"), "Deed", open_book.id)
    ocr.during = lambda: intake.replace_signed_copy(
        me, doc.document_id, pdf("right-scan"), "Wrong scan"
    )
    ocr.error = http_error(400)

    await _extract(session, doc.document_id)

    assert (await _doc(session, doc.document_id)).status == STATUS_QUEUED
    assert notified == []


async def test_replace_during_enrichment_wins_over_the_running_job(
    session, worker_db, ocr, analyzer, notified, intake, lawyer, open_book
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("wrong-scan"), "Deed", open_book.id)
    await _extract(session, doc.document_id)
    await ExtractionReviewService(session, RecordingQueue()).approve(me, doc.document_id)
    analyzer.during = lambda: intake.replace_signed_copy(
        me, doc.document_id, pdf("right-scan"), "Wrong scan"
    )

    await _enrich(doc.document_id, analyzer)

    assert (await _doc(session, doc.document_id)).status == STATUS_QUEUED
    for model in (DocumentExtraction, DocumentChunk, DocumentInsight):
        assert await _count(session, model, doc.document_id) == 0
    assert "completed" not in notified


# === Analysis never looks safer than it is ===


async def test_a_failed_analysis_fails_enrichment_instead_of_showing_no_risks(
    session, worker_db, ocr, notified, intake, queries, lawyer, open_book
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await _process(session, lawyer, doc.document_id, FailingAnalyzer())

    doc = await queries.get_document(me, doc.document_id)
    assert doc.status == STATUS_FAILED
    assert not doc.permissions.can_finalize
    assert doc.permissions.can_retry_processing
    assert await _count(session, DocumentInsight, doc.document_id) == 0


def _analysis(**fields) -> str:
    import json

    return json.dumps({"summary": None, "labels": [], "entities": [], "risk_flags": [], **fields})


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch):
    def install(*answers: str) -> ScriptedLLM:
        fake = ScriptedLLM(*answers)
        monkeypatch.setattr(llm_analysis, "build_llm", lambda model=None: fake)
        return fake

    return install


async def test_a_long_document_is_analyzed_to_the_end(llm):
    early = "Recital. " * 3_000
    late = "The seller may cancel at will and keep the deposit."
    text = f"{early}\n\n{late}"
    assert len(text) > llm_analysis.MAX_SECTION_CHARS
    fake = llm(_analysis(summary="A deed."), _analysis(risk_flags=[RISK]))

    result = await llm_analysis.LLMAnalysisProvider().analyze(text)

    assert late in fake.sent[-1]
    assert all(len(s) <= llm_analysis.MAX_SECTION_CHARS for s in fake.sent)
    assert result.risk_flags == [RISK]
    assert result.summary == "A deed."
    assert result.meta["analysis_status"] == ANALYSIS_STATUS_COMPLETE
    assert result.meta["sections"] == 2


async def test_unparseable_output_is_retried_then_raised(llm):
    fake = llm("not json", "still not json")

    with pytest.raises(AnalysisFailed):
        await llm_analysis.LLMAnalysisProvider().analyze("A short deed.")
    assert len(fake.sent) == 2


async def test_a_section_that_cannot_be_read_marks_the_analysis_partial(llm):
    text = ("Recital. " * 3_000) + "\n\n" + "The seller may cancel at will."
    llm("garbage", "garbage", _analysis(risk_flags=[RISK]))

    result = await llm_analysis.LLMAnalysisProvider().analyze(text)

    assert result.meta["analysis_status"] == ANALYSIS_STATUS_PARTIAL
    assert result.meta["failed_sections"] == [0]
    assert result.risk_flags == [RISK]


def test_sections_keep_every_character_of_a_huge_paragraph():
    text = "x" * (llm_analysis.MAX_SECTION_CHARS * 2 + 5)
    sections = llm_analysis.split_into_sections(text)
    assert "".join(sections) == text
    assert all(len(s) <= llm_analysis.MAX_SECTION_CHARS for s in sections)


def test_analysis_failure_is_retried_by_the_worker():
    assert worker.is_retryable(AnalysisFailed("unreadable"))


async def test_retry_after_failed_ocr_still_works_at_signed(
    session, worker_db, ocr, notified, intake, lawyer, open_book
):
    from app.features.documents.workflow.processing import DocumentProcessing

    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    ocr.error = httpx.ConnectError("ocr down")
    await _extract(session, doc.document_id)
    assert (await _doc(session, doc.document_id)).status == STATUS_FAILED

    queue = RecordingQueue()
    await DocumentProcessing(session, queue).retry(me, doc.document_id)
    assert queue.copies == [doc.signed_copy.id]
    ocr.error = None
    await _extract(session, doc.document_id)
    assert (await _doc(session, doc.document_id)).status == STATUS_AWAITING_REVIEW
