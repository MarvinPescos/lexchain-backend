"""Attaching the signed copy (Path 1) — issue #25.

The stage-change insert failed on a database a migration behind, after the
signed copy was stored and the register entry claimed. These pin what a
successful attach persists and what a failed one must leave behind: nothing.
"""

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError

from app.features.documents.models import Document, DocumentStageChange, SignedCopy
from tests.conftest import as_auth, connect_google, pdf


@pytest.fixture
async def ready(intake, drafting, google, lawyer):
    me = as_auth(lawyer)
    await connect_google(google, lawyer)
    doc = await intake.open_document(me, "Deed of Sale – Reyes lot")
    await drafting.create_draft(me, doc.document_id)
    return await drafting.mark_ready(me, doc.document_id)


async def _stage_changes(session, document_id) -> list[DocumentStageChange]:
    rows = await session.scalars(
        select(DocumentStageChange)
        .where(DocumentStageChange.document_id == document_id)
        .order_by(DocumentStageChange.created_at)
    )
    return list(rows)


async def test_attach_records_the_transition(session, intake, lawyer, open_book, ready):
    doc = await intake.attach_signed_copy(
        as_auth(lawyer), ready.document_id, pdf("reyes-signed"), open_book.id
    )
    assert (doc.lifecycle, doc.book_id, doc.doc_no, doc.page_no) == (
        "SIGNED", open_book.id, 1, 1,
    )

    last = (await _stage_changes(session, ready.document_id))[-1]
    assert (last.action, last.from_stage, last.to_stage, last.actor_id) == (
        "attach_signed_copy", "READY_FOR_SIGNATURE", "SIGNED", lawyer.id,
    )
    assert (last.from_hash, last.to_hash) == (None, None)  # restore-only


async def test_failed_stage_change_leaves_nothing_behind(
    session, intake, lawyer, open_book, ready, storage, monkeypatch
):
    me, book_id = as_auth(lawyer), open_book.id  # the rollback expires both
    add = intake.repo.add

    async def missing_column(row):
        if isinstance(row, DocumentStageChange):
            raise ProgrammingError(
                "INSERT INTO document_stage_changes ...", {},
                Exception('column "from_hash" does not exist'),
            )
        return await add(row)

    monkeypatch.setattr(intake.repo, "add", missing_column)
    with pytest.raises(ProgrammingError):
        await intake.attach_signed_copy(me, ready.document_id, pdf("reyes-signed"), book_id)
    await session.rollback()  # what get_db does with the request's session

    document = await session.get(Document, ready.document_id, populate_existing=True)
    assert (document.lifecycle, document.book_id, document.doc_no) == (
        "READY_FOR_SIGNATURE", None, None,
    )
    copies = await session.scalar(
        select(func.count()).select_from(SignedCopy)
        .where(SignedCopy.document_id == ready.document_id)
    )
    assert copies == 0
    assert storage.objects == {}
    assert [c.action for c in await _stage_changes(session, ready.document_id)] == [
        "open", "create_draft", "mark_ready",
    ]

    # The retry files into the same entry — no duplicate, no gap.
    monkeypatch.setattr(intake.repo, "add", add)
    doc = await intake.attach_signed_copy(me, ready.document_id, pdf("reyes-signed"), book_id)
    assert (doc.lifecycle, doc.doc_no, doc.page_no) == ("SIGNED", 1, 1)
    assert len(storage.objects) == 1
