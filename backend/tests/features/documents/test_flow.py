"""Both entry paths, end to end through DocumentService."""

import hashlib

import pytest

from app.features.documents.models import DocumentParty
from app.shared.constants import PARTY_STATUS_ACCEPTED
from app.shared.errors.exceptions import ConflictError, ForbiddenError, NotFoundError
from tests.conftest import as_auth, connect_google, mark_processed, pdf, real_pdf



async def test_path1_prepare_sign_attach_finalize(
    session, queries, intake, drafting, finalization, google, lawyer, open_book, storage, queue
):
    me = as_auth(lawyer)
    await connect_google(google, lawyer)

    doc = await intake.open_document(me, "Deed of Sale – Cruz lot")
    assert (doc.lifecycle, doc.book_id, doc.signed_copy) == ("PREPARING", None, None)
    assert doc.permissions.can_create_draft

    doc = await drafting.create_draft(me, doc.document_id)
    assert doc.draft_url.startswith("https://docs.google.com/document/d/")
    assert not doc.permissions.can_create_draft
    assert doc.permissions.can_mark_ready
    assert not doc.permissions.can_attach_signed_copy

    with pytest.raises(ConflictError, match="Mark the document Ready"):
        await intake.attach_signed_copy(me, doc.document_id, pdf(), open_book.id)

    doc = await drafting.mark_ready(me, doc.document_id)
    assert doc.lifecycle == "READY_FOR_SIGNATURE"

    doc = await intake.attach_signed_copy(
        me, doc.document_id, pdf("cruz-signed"), open_book.id
    )
    assert (doc.lifecycle, doc.doc_no, doc.page_no) == ("SIGNED", 1, 1)
    assert doc.signed_copy.is_current
    # Processed while SIGNED, so the risks are seen before finalize.
    assert doc.status == "QUEUED"
    assert queue.processed == [doc.document_id]
    assert not doc.permissions.can_finalize

    await mark_processed(session, doc.document_id)
    record = await finalization.finalize(me, doc.document_id)
    [stored] = storage.objects.values()
    assert record.data_hash == hashlib.sha256(stored).hexdigest()
    assert queue.processed == [doc.document_id]  # finalize queues nothing

    doc = await queries.get_document(me, doc.document_id)
    assert (doc.lifecycle, doc.status, doc.on_chain) == ("FINALIZED", "COMPLETED", True)

    history = await queries.get_history(me, doc.document_id)
    assert [c.action for c in history.changes] == [
        "open", "create_draft", "mark_ready", "attach_signed_copy", "finalize",
    ]


async def test_path2_upload_then_finalize(
    session, queries, intake, finalization, lawyer, closed_book, queue
):
    me = as_auth(lawyer)

    doc = await intake.upload_signed(
        me, pdf("old-deed"), "Old deed (2025)", closed_book.id, doc_no=45, page_no=9
    )
    assert (doc.lifecycle, doc.doc_no, doc.page_no, doc.status) == (
        "SIGNED", 45, 9, "QUEUED",
    )
    assert queue.processed == [doc.document_id]

    await mark_processed(session, doc.document_id)
    await finalization.finalize(me, doc.document_id)
    doc = await queries.get_document(me, doc.document_id)
    assert doc.lifecycle == "FINALIZED"


async def test_finalize_notifies_the_lawyer_and_the_clients(
    session, intake, finalization, lawyer, client, closed_book, monkeypatch
):
    import app.celery_tasks as tasks

    notified: list[str] = []
    monkeypatch.setattr(
        tasks.handle_document_recorded, "delay", lambda **kw: notified.append(kw["user_id"])
    )
    me = as_auth(lawyer)
    doc = await intake.upload_signed(
        me, pdf("cruz-deed"), "Cruz deed", closed_book.id, doc_no=46, page_no=9
    )
    session.add(
        DocumentParty(
            document_id=doc.document_id,
            user_id=client.id,
            email=client.email,
            status=PARTY_STATUS_ACCEPTED,
        )
    )
    await session.flush()

    await mark_processed(session, doc.document_id)
    await finalization.finalize(me, doc.document_id)
    assert sorted(notified) == sorted([str(lawyer.id), str(client.id)])


async def test_closed_book_requires_numbers(intake, lawyer, closed_book):
    from app.shared.errors.exceptions import BadRequestError

    with pytest.raises(BadRequestError, match="closed"):
        await intake.upload_signed(
            as_auth(lawyer), pdf(), "Old deed", closed_book.id
        )


async def test_upload_is_stopped_while_a_document_waits_for_its_signed_copy(
    intake, drafting, lawyer, open_book
):
    me = as_auth(lawyer)
    waiting = await intake.open_document(me, "Deed of Sale – Cruz lot")
    await drafting.mark_ready(me, waiting.document_id)

    with pytest.raises(ConflictError) as refused:
        await intake.upload_signed(me, pdf(), "Cruz deed", open_book.id)
    assert refused.value.details["reason"] == "READY_FOR_SIGNATURE_EXISTS"
    assert refused.value.details["documents"][0]["document_id"] == str(
        waiting.document_id
    )

    doc = await intake.upload_signed(
        me, pdf(), "Unrelated deed", open_book.id, confirm_new_record=True
    )
    assert doc.lifecycle == "SIGNED"


async def test_same_file_cannot_be_filed_twice(intake, lawyer, open_book):
    me = as_auth(lawyer)
    first = await intake.upload_signed(me, pdf("x"), "First", open_book.id)

    with pytest.raises(ConflictError) as refused:
        await intake.upload_signed(me, pdf("x"), "Again", open_book.id)
    assert refused.value.details == {
        "reason": "DUPLICATE_FILE",
        "document_id": str(first.document_id),
    }


async def test_rejected_filing_leaves_nothing_stored(
    intake, lawyer, closed_book, storage
):
    me = as_auth(lawyer)
    await intake.upload_signed(
        me, pdf("a"), "A", closed_book.id, doc_no=7, page_no=1
    )
    with pytest.raises(ConflictError, match="Doc. No. 7"):
        await intake.upload_signed(
            me, pdf("b"), "B", closed_book.id, doc_no=7, page_no=1
        )
    assert len(storage.objects) == 1


async def test_replace_keeps_entry_and_old_copy(queries, intake, lawyer, open_book):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("wrong-scan"), "Deed", open_book.id)

    replaced = await intake.replace_signed_copy(
        me, doc.document_id, pdf("right-scan"), reason="Scanned the unsigned page"
    )
    assert (replaced.doc_no, replaced.page_no) == (doc.doc_no, doc.page_no)
    assert replaced.document_hash != doc.document_hash

    copies = await queries.get_signed_copies(me, doc.document_id)
    assert [c.is_current for c in copies.copies] == [True, False]
    assert copies.copies[0].replaced_reason == "Scanned the unsigned page"


async def test_correct_entry(intake, lawyer, closed_book):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(
        me, pdf(), "Deed", closed_book.id, doc_no=45, page_no=9
    )
    fixed = await intake.correct_entry(me, doc.document_id, 54, 11)
    assert (fixed.doc_no, fixed.page_no) == (54, 11)


async def test_cancel_only_before_signing(intake, drafting, lawyer, open_book):
    me = as_auth(lawyer)
    draft = await intake.open_document(me, "Draft")
    cancelled = await drafting.cancel(me, draft.document_id)
    assert cancelled.lifecycle == "CANCELLED"

    signed = await intake.upload_signed(me, pdf(), "Signed", open_book.id)
    with pytest.raises(ConflictError):
        await drafting.cancel(me, signed.document_id)


async def test_verify_detects_a_changed_file(
    session, intake, finalization, verification, lawyer, open_book, storage
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    await finalization.finalize(me, doc.document_id)
    verify = verification.verify_document

    assert (await verify(me, doc.document_id)).status == "AUTHENTIC"

    [url] = storage.objects
    storage.objects[url] += b" "
    assert (await verify(me, doc.document_id)).status == "TAMPERED"


async def test_party_reads_but_cannot_act(
    queries, intake, drafting, session, lawyer, client, open_book
):
    me = as_auth(lawyer)
    doc = await intake.open_document(me, "Deed")
    session.add(
        DocumentParty(
            document_id=doc.document_id,
            user_id=client.id,
            email=client.email,
            status=PARTY_STATUS_ACCEPTED,
        )
    )
    await session.flush()

    seen = await queries.get_document(as_auth(client), doc.document_id)
    assert not seen.permissions.can_mark_ready
    with pytest.raises(ForbiddenError):
        await drafting.mark_ready(as_auth(client), doc.document_id)


async def test_pdf_is_read_through_a_short_lived_link_after_an_access_check(
    queries, intake, sharing, session, lawyer, client, other_lawyer, open_book, storage
):
    me = as_auth(lawyer)
    first = await intake.upload_signed(me, real_pdf("scan-1"), "Deed", open_book.id)
    doc = await intake.replace_signed_copy(
        me, first.document_id, real_pdf("scan-2"), "Wrong scan"
    )
    assert "storage_url" not in doc.signed_copy.model_dump()

    current = await queries.get_signed_copy_file(me, doc.document_id)
    assert current.copy_id == doc.signed_copy.id
    assert "token=signed" in current.url and current.expires_in == 300
    older = await queries.get_signed_copy_file(me, doc.document_id, first.signed_copy.id)
    assert older.copy_id == first.signed_copy.id

    # Never shared: nothing to read.
    with pytest.raises(NotFoundError):
        await queries.get_signed_copy_file(as_auth(other_lawyer), doc.document_id)

    # A party reads while on the document, and not once removed.
    session.add(
        DocumentParty(
            document_id=doc.document_id,
            user_id=client.id,
            email=client.email,
            status=PARTY_STATUS_ACCEPTED,
        )
    )
    await session.flush()
    # Unwatermarked only once an e-copy request is approved (test_ecopy).
    assert (await queries.get_signed_copy_file(as_auth(client), doc.document_id)).watermarked

    await sharing.remove_party(me, doc.document_id, client.id)
    with pytest.raises(NotFoundError):
        await queries.get_signed_copy_file(as_auth(client), doc.document_id)


async def test_removed_party_is_notified(
    intake, sharing, session, lawyer, client, monkeypatch
):
    import app.celery_tasks as tasks

    sent: list[dict] = []
    monkeypatch.setattr(
        tasks.handle_party_removed, "delay", lambda **k: sent.append(k)
    )
    me = as_auth(lawyer)
    doc = await intake.open_document(me, "Deed of Sale – Cruz lot")
    session.add(
        DocumentParty(
            document_id=doc.document_id,
            user_id=client.id,
            email=client.email,
            status=PARTY_STATUS_ACCEPTED,
        )
    )
    await session.flush()

    await sharing.remove_party(me, doc.document_id, client.id)

    assert sent == [
        {
            "party_user_id": str(client.id),
            "owner_name": f"{lawyer.f_name} {lawyer.l_name}",
            "document_title": "Deed of Sale – Cruz lot",
            "document_id": str(doc.document_id),
            "was_pending": False,
        }
    ]
