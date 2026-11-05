"""Verdicts of a check against the chain when the file or `lifecycle` was attacked."""

import pytest
from storage3.utils import StorageException
from structlog.testing import capture_logs

from app.features.blockchain.service import BlockchainService
from app.features.documents.models import Document
from app.features.documents.workflow.finalization import DocumentFinalization
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.shared.errors.exceptions import NotFoundError, ServiceUnavailableError
from tests.conftest import as_auth, pdf, mark_processed


@pytest.fixture
def verifier(session, storage, chain) -> BlockchainService:
    return BlockchainService(session, chain)


@pytest.fixture
async def finalized(session, intake, queue, chain, storage, lawyer, open_book):
    """A finalized deed; returns (document_id, stored url)."""
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    await DocumentFinalization(session, chain).finalize(me, doc.document_id)
    [url] = storage.objects
    return doc.document_id, url


async def _alter_snapshot(session, document_id) -> None:
    snapshot = await DocumentSnaphotRepository(session).get_by_document_id(document_id)
    snapshot.file_sha256 = "0" * 64
    await session.flush()


# === Deleted file ===


async def test_deleted_file_is_reported_as_missing(verifier, finalized, storage, lawyer):
    document_id, url = finalized
    del storage.objects[url]

    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.status == "FILE_MISSING"
    assert result.is_authentic is False
    assert result.baseline_trusted is True
    assert result.current_hash is None
    assert result.onchain_hash is not None
    assert result.tamper_report_status == "NOT_APPLICABLE"


@pytest.mark.parametrize(
    "outage", [OSError("storage unreachable"), StorageException("internal error")]
)
async def test_storage_outage_is_not_a_missing_file(
    verifier, finalized, storage, lawyer, outage
):
    document_id, _ = finalized

    async def down(_url):
        raise outage

    storage.download = down

    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.status == "VERIFICATION_UNAVAILABLE"


async def test_deleted_file_with_an_altered_snapshot_is_compromised(
    session, verifier, finalized, storage, lawyer
):
    document_id, url = finalized
    del storage.objects[url]
    await _alter_snapshot(session, document_id)

    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.status == "SNAPSHOT_COMPROMISED"
    assert result.is_authentic is False
    assert result.baseline_trusted is False
    assert result.current_hash is None


# === Chain read before `lifecycle` ===


async def _edit_lifecycle(session, document_id) -> None:
    document = await session.get(Document, document_id)
    document.lifecycle = "SIGNED"
    await session.flush()


async def test_edited_lifecycle_does_not_hide_a_tampered_file(
    session, verifier, finalized, storage, lawyer
):
    document_id, url = finalized
    storage.objects[url] += b" tampered"
    await _edit_lifecycle(session, document_id)

    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.status == "TAMPERED"


async def test_edited_lifecycle_over_a_genuine_file_is_authentic_and_logged(
    session, verifier, finalized, lawyer
):
    document_id, _ = finalized
    await _edit_lifecycle(session, document_id)

    with capture_logs() as logs:
        result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.status == "AUTHENTIC"
    [mismatch] = [e for e in logs if e["event"] == "blockchain.verify.lifecycle_mismatch"]
    assert mismatch["lifecycle"] == "SIGNED"


async def test_draft_is_not_anchored_without_retrying_the_empty_read(
    verifier, intake, chain, lawyer, open_book
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("draft"), "Draft", open_book.id)

    result = await verifier.verify_document(me, doc.document_id)

    assert result.status == "NOT_ANCHORED"
    assert chain.retry_empty_reads == [False]


async def test_finalized_document_retries_the_empty_read(
    verifier, finalized, chain, lawyer
):
    document_id, _ = finalized

    await verifier.verify_document(as_auth(lawyer), document_id)

    assert chain.retry_empty_reads == [True]


async def test_finalized_document_with_no_chain_record_is_not_found(
    verifier, finalized, chain, lawyer
):
    document_id, _ = finalized
    chain.records.clear()

    with pytest.raises(NotFoundError):
        await verifier.verify_document(as_auth(lawyer), document_id)


async def test_chain_outage_on_a_draft_is_unavailable(
    verifier, intake, chain, lawyer, open_book
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("draft"), "Draft", open_book.id)

    async def down(*args, **kwargs):
        raise ServiceUnavailableError("The blockchain could not be reached.")

    chain.get_document_hash = down

    result = await verifier.verify_document(me, doc.document_id)

    assert result.status == "VERIFICATION_UNAVAILABLE"
    assert result.is_authentic is False


# === Orphaned anchor ===


@pytest.fixture
async def orphaned(intake, chain, lawyer, open_book):
    """Anchored, then finalize failed: the rollback left a draft and no snapshot."""
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("orphan"), "Orphan", open_book.id)
    await chain.store_document_hash(str(doc.document_id), "ab" * 32)
    return doc.document_id


async def test_orphaned_anchor_is_unavailable(verifier, orphaned, lawyer):
    result = await verifier.verify_document(as_auth(lawyer), orphaned)

    assert result.status == "VERIFICATION_UNAVAILABLE"
    assert result.is_authentic is False
    assert result.onchain_hash == "ab" * 32


async def test_anchor_with_no_signed_copy_is_file_missing(
    session, verifier, finalized, lawyer
):
    document_id, _ = finalized
    document = await session.get(Document, document_id)
    document.signed_copy_id = None
    await _edit_lifecycle(session, document_id)

    result = await verifier.verify_document(as_auth(lawyer), document_id)

    assert result.status == "FILE_MISSING"


async def test_finalized_document_with_no_snapshot_is_not_found(
    session, verifier, finalized, lawyer
):
    document_id, _ = finalized
    snapshot = await DocumentSnaphotRepository(session).get_by_document_id(document_id)
    await session.delete(snapshot)
    await session.flush()

    with pytest.raises(NotFoundError):
        await verifier.verify_document(as_auth(lawyer), document_id)
