"""The tamper report: where a TAMPERED document changed, against a trusted baseline."""

import pytest

from app.features.blockchain.service import BlockchainService
from app.features.documents.workflow.finalization import DocumentFinalization
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.infrastructure.nlp.ocr.base import ExtractionResult
from tests.conftest import as_auth, pdf, mark_processed


class FakeBackup:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def store(self, key, data, content_type):
        self.objects[key] = data

    async def fetch(self, key):
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]

    async def exists(self, key):
        return key in self.objects


class FakeOCR:
    """Returns the extraction registered for the exact bytes it is given."""

    def __init__(self) -> None:
        self.results: dict[bytes, ExtractionResult | Exception] = {}
        self.calls: list[bytes] = []

        self.built_for: list[bytes] = []

    def build(self, file_data: bytes) -> "FakeOCR":
        self.built_for.append(file_data)
        return self

    async def extract(self, file_data: bytes) -> ExtractionResult:
        self.calls.append(file_data)
        result = self.results[file_data]
        if isinstance(result, Exception):
            raise result
        return result


def block(index: int, text: str, page: int = 0) -> dict:
    return {
        "index": index,
        "editable": True,
        "type": "text",
        "text": text,
        "original_text": text,
        "bbox": [100.0, 100.0 + index * 50, 900.0, 140.0 + index * 50],
        "page_idx": page,
    }


def laid_out(*texts: str) -> ExtractionResult:
    blocks = [block(i, t) for i, t in enumerate(texts)]
    text = "\n\n".join(texts)
    return ExtractionResult(text=text, ingestion_text=text, page_count=1, blocks=blocks)


def text_only(*texts: str) -> ExtractionResult:
    text = "\n\n".join(texts)
    return ExtractionResult(text=text, ingestion_text=text, page_count=1)


ORIGINAL = (
    "DEED OF ABSOLUTE SALE",
    "The price is PHP 500,000.00.",
    "Signed in Manila.",
)


@pytest.fixture
def backup() -> FakeBackup:
    return FakeBackup()


@pytest.fixture
def ocr() -> FakeOCR:
    return FakeOCR()


@pytest.fixture
def verifier(session, storage, chain, backup, ocr) -> BlockchainService:
    return BlockchainService(session, chain, backup, ocr.build)


@pytest.fixture
async def finalized(session, intake, queue, chain, backup, storage, lawyer, open_book):
    """A finalized, archived deed; returns (document_id, stored url)."""
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    await DocumentFinalization(session, chain, backup).finalize(me, doc.document_id)
    [url] = storage.objects
    return doc.document_id, url


async def _tamper(storage, ocr, url, current: ExtractionResult | Exception) -> None:
    ocr.results[storage.objects[url]] = laid_out(*ORIGINAL)
    storage.objects[url] = storage.objects[url] + b" tampered"
    ocr.results[storage.objects[url]] = current


async def test_replacement_is_located_on_the_current_page(
    verifier, finalized, storage, ocr, lawyer
):
    document_id, url = finalized
    await _tamper(
        storage,
        ocr,
        url,
        laid_out(ORIGINAL[0], "The price is PHP 50,000.00.", ORIGINAL[2]),
    )

    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.status == "TAMPERED"
    assert result.tamper_report_status == "AVAILABLE"
    report = result.tamper_report
    assert report.localized is True
    assert report.replaced == 1
    assert report.inserted == report.deleted == 0
    [segment] = report.segments
    assert segment.type == "replace"
    assert segment.severity == "critical"
    assert segment.original_text == "The price is PHP 500,000.00."
    assert segment.current_text == "The price is PHP 50,000.00."
    assert segment.block_index == 1
    assert segment.original_block_index == 1
    assert segment.location == "current"
    assert segment.page_idx == 0
    assert segment.bbox == [100.0, 150.0, 900.0, 190.0]


async def test_insertion_is_reported(verifier, finalized, storage, ocr, lawyer):
    document_id, url = finalized
    await _tamper(
        storage,
        ocr,
        url,
        laid_out(ORIGINAL[0], ORIGINAL[1], "Buyer waives all warranties.", ORIGINAL[2]),
    )

    report = (await verifier.verify_document(as_auth(lawyer), document_id)).tamper_report

    assert report.inserted == 1
    [segment] = report.segments
    assert segment.type == "insert"
    assert segment.original_text == ""
    assert segment.current_text == "Buyer waives all warranties."
    assert segment.block_index == 2
    assert segment.location == "current"
    assert segment.bbox == [100.0, 200.0, 900.0, 240.0]


async def test_deletion_points_at_the_anchored_original(
    verifier, finalized, storage, ocr, lawyer
):
    document_id, url = finalized
    await _tamper(storage, ocr, url, laid_out(ORIGINAL[0], ORIGINAL[2]))

    report = (await verifier.verify_document(as_auth(lawyer), document_id)).tamper_report

    assert report.deleted == 1
    [segment] = report.segments
    assert segment.type == "delete"
    assert segment.original_text == "The price is PHP 500,000.00."
    assert segment.current_text == ""
    assert segment.original_block_index == 1
    assert segment.location == "original"
    assert segment.page_idx == 0
    assert segment.bbox == [100.0, 150.0, 900.0, 190.0]


async def test_text_diff_without_layout_has_null_coordinates(
    verifier, finalized, storage, ocr, lawyer
):
    document_id, url = finalized
    ocr.results[storage.objects[url]] = text_only(*ORIGINAL)
    storage.objects[url] += b" tampered"
    ocr.results[storage.objects[url]] = text_only(
        ORIGINAL[0], "The price is PHP 50,000.00.", ORIGINAL[2]
    )

    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.tamper_report_status == "AVAILABLE"
    assert result.tamper_report.localized is False
    [segment] = result.tamper_report.segments
    assert segment.type == "replace"
    assert segment.current_text == "The price is PHP 50,000.00."
    assert segment.page_idx is None
    assert segment.bbox is None


async def test_both_sides_are_read_by_the_engine_chosen_for_the_original(
    verifier, finalized, storage, ocr, lawyer
):
    document_id, url = finalized
    original = storage.objects[url]
    await _tamper(storage, ocr, url, laid_out(ORIGINAL[0], ORIGINAL[2]))

    await verifier.verify_document(as_auth(lawyer), document_id)

    assert ocr.built_for == [original]
    assert ocr.calls == [original, storage.objects[url]]


async def test_an_engine_that_fell_back_mid_check_rereads_the_original(
    verifier, finalized, storage, ocr, lawyer
):
    document_id, url = finalized
    original = storage.objects[url]
    await _tamper(storage, ocr, url, laid_out(ORIGINAL[0], ORIGINAL[2]))
    on_marker = laid_out(*ORIGINAL).model_copy(update={"meta": {"engine": "marker-api"}})
    on_mineru = laid_out(*ORIGINAL).model_copy(update={"meta": {"engine": "mineru-api"}})
    current = ocr.results[storage.objects[url]].model_copy(
        update={"meta": {"engine": "mineru-api"}}
    )
    ocr.results[storage.objects[url]] = current
    reads = iter([on_marker, on_mineru])
    extract = ocr.extract

    async def fall_back_after_the_first_read(file_data: bytes) -> ExtractionResult:
        if file_data == original:
            ocr.calls.append(file_data)
            return next(reads)
        return await extract(file_data)

    ocr.extract = fall_back_after_the_first_read

    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert ocr.calls == [original, storage.objects[url], original]
    assert result.tamper_report.deleted == 1
    assert result.tamper_report.replaced == result.tamper_report.inserted == 0


async def test_change_outside_the_text_is_not_reported_as_no_change(
    verifier, finalized, storage, ocr, lawyer
):
    document_id, url = finalized
    await _tamper(storage, ocr, url, laid_out(*ORIGINAL))

    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.status == "TAMPERED"
    assert result.tamper_report_status == "UNAVAILABLE"
    assert result.tamper_report is None


async def test_ocr_spacing_drift_is_not_a_change(
    verifier, finalized, storage, ocr, lawyer
):
    document_id, url = finalized
    await _tamper(
        storage,
        ocr,
        url,
        laid_out(
            "DEED OF  ABSOLUTE SALE",
            "The price is PHP 50,000.00.",
            "Signed in Manila .",
        ),
    )

    report = (await verifier.verify_document(as_auth(lawyer), document_id)).tamper_report

    [segment] = report.segments
    assert segment.original_text == "The price is PHP 500,000.00."


async def test_capped_report_still_counts_every_change(
    verifier, finalized, storage, ocr, lawyer, monkeypatch
):
    from app.features.blockchain import service, tamper

    monkeypatch.setattr(
        service, "diff_blocks", lambda a, b: tamper.diff_blocks(a, b, max_segments=1)
    )
    document_id, url = finalized
    await _tamper(storage, ocr, url, laid_out("Changed title", ORIGINAL[1]))

    report = (await verifier.verify_document(as_auth(lawyer), document_id)).tamper_report

    assert report.truncated is True
    assert report.total_changes == 1
    assert report.replaced + report.deleted == 2


async def test_malformed_extraction_keeps_the_verdict(
    verifier, finalized, storage, ocr, lawyer
):
    document_id, url = finalized
    broken = laid_out("Changed title")
    broken.blocks[0]["bbox"] = "not a box"
    await _tamper(storage, ocr, url, broken)

    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.status == "TAMPERED"
    assert result.tamper_report_status == "UNAVAILABLE"


async def test_unreadable_current_file_keeps_the_verdict(
    verifier, finalized, storage, ocr, lawyer
):
    document_id, url = finalized
    await _tamper(storage, ocr, url, RuntimeError("OCR engine down"))

    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.status == "TAMPERED"
    assert result.is_authentic is False
    assert result.tamper_report_status == "UNAVAILABLE"
    assert result.tamper_report is None
    assert result.tamper_report_unavailable_reason


async def test_missing_archive_reports_the_diff_unavailable(
    verifier, finalized, storage, ocr, backup, lawyer
):
    document_id, url = finalized
    await _tamper(storage, ocr, url, laid_out(ORIGINAL[0]))
    backup.objects.clear()

    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.status == "TAMPERED"
    assert result.tamper_report_status == "UNAVAILABLE"
    assert result.tamper_report is None


async def test_archive_that_does_not_match_the_chain_is_not_a_baseline(
    verifier, finalized, storage, ocr, backup, lawyer
):
    document_id, url = finalized
    await _tamper(storage, ocr, url, laid_out(ORIGINAL[0]))
    [key] = [k for k in backup.objects if k.endswith(".pdf")]
    backup.objects[key] = storage.objects[url]

    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.status == "TAMPERED"
    assert result.tamper_report_status == "UNAVAILABLE"
    assert result.tamper_report is None


async def test_untrusted_snapshot_claims_no_diff(
    session, verifier, finalized, storage, ocr, lawyer
):
    document_id, url = finalized
    await _tamper(storage, ocr, url, laid_out(ORIGINAL[0]))
    snapshot = await DocumentSnaphotRepository(session).get_by_document_id(document_id)
    snapshot.file_sha256 = "0" * 64
    await session.flush()

    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.status == "SNAPSHOT_COMPROMISED"
    assert result.tamper_report_status == "NOT_APPLICABLE"
    assert result.tamper_report is None
    assert ocr.calls == []


async def test_authentic_document_claims_no_diff(verifier, finalized, ocr, lawyer):
    document_id, _ = finalized

    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.status == "AUTHENTIC"
    assert result.tamper_report_status == "NOT_APPLICABLE"
    assert result.tamper_report is None
    assert ocr.calls == []


async def test_altered_snapshot_over_a_genuine_file_stays_authentic(
    session, verifier, finalized, lawyer
):
    document_id, _ = finalized
    snapshot = await DocumentSnaphotRepository(session).get_by_document_id(document_id)
    snapshot.file_sha256 = "0" * 64
    await session.flush()

    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.status == "AUTHENTIC"
    assert result.baseline_trusted is False


async def test_unavailable_verification_claims_no_diff(
    verifier, finalized, storage, ocr, lawyer
):
    document_id, url = finalized
    del storage.objects[url]

    async def down(_url):
        raise OSError("storage unreachable")

    storage.download = down
    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.status == "VERIFICATION_UNAVAILABLE"
    assert result.tamper_report_status == "NOT_APPLICABLE"
    assert result.tamper_report is None
    assert ocr.calls == []


async def test_not_anchored_document_claims_no_diff(
    verifier, intake, ocr, lawyer, open_book
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("draft"), "Draft", open_book.id)

    result = await verifier.verify_document(me, doc.document_id)

    assert result.status == "NOT_ANCHORED"
    assert result.tamper_report_status == "NOT_APPLICABLE"
    assert result.tamper_report is None


def _blocks(*texts):
    return [
        {"index": i, "text": t, "page_idx": 0, "bbox": [0, i, 1, i + 1]}
        for i, t in enumerate(texts)
    ]


def test_deleted_block_is_not_compared_with_the_block_below():
    from app.features.blockchain.tamper import diff_blocks

    original = _blocks(
        "DEED OF ABSOLUTE SALE",
        "Signed in Manila.",
        "Doc. No. 12; Page No. 3; Book No. I",
    )
    # The block above Doc No is gone and OCR read Doc No slightly differently.
    current = _blocks("DEED OF ABSOLUTE SALE", "Doc. No. 12, Page No. 3; Book No. I")

    segments = sorted(
        diff_blocks(original, current)["segments"],
        key=lambda s: s["original_line_start"],
    )

    assert [(s["type"], s["original_text"], s["current_text"]) for s in segments] == [
        ("delete", "Signed in Manila.", ""),
        (
            "replace",
            "Doc. No. 12; Page No. 3; Book No. I",
            "Doc. No. 12, Page No. 3; Book No. I",
        ),
    ]


def test_block_read_in_another_order_is_compared_with_itself():
    from app.features.blockchain.tamper import diff_blocks

    original = _blocks(
        "IN WITNESS WHEREOF, I have hereunto set my hand",
        "Daniela Mae Villareal (Signature of Affiant)",
        "JURAT",
        "Doc. No. 6, Page No. 6 Book No. 1, Series of 2026",
    )
    # The signature is gone, and OCR now reads Doc No before JURAT.
    current = _blocks(
        "IN WITNESS WHEREOF, I have hereunto set my hand",
        "Doc. No. 6. Page No. 6. Book No. 1, Series of 2026",
        "JURAT",
    )

    segments = sorted(
        diff_blocks(original, current)["segments"],
        key=lambda s: s["original_line_start"],
    )

    assert [(s["type"], s["original_text"], s["current_text"]) for s in segments] == [
        ("delete", "Daniela Mae Villareal (Signature of Affiant)", ""),
        (
            "replace",
            "Doc. No. 6, Page No. 6 Book No. 1, Series of 2026",
            "Doc. No. 6. Page No. 6. Book No. 1, Series of 2026",
        ),
    ]
