"""GET /books/{book_id}/entries — the documents filed in one register book."""

import uuid

import httpx
import pytest

from app.features.books.models import Book
from app.features.documents.constants import (
    LIFECYCLE_FINALIZED,
    LIFECYCLE_PREPARING,
    LIFECYCLE_SIGNED,
    STATUS_COMPLETED,
)
from app.features.documents.models import Document
from app.infrastructure.database import get_db
from app.infrastructure.security.dependencies import get_current_user
from app.main import app
from tests.conftest import as_auth


@pytest.fixture
def http(session):
    """Call the API as a given user, inside the test's transaction."""

    async def same_session():
        yield session

    async def call(user, path, **params):
        app.dependency_overrides[get_db] = same_session
        app.dependency_overrides[get_current_user] = lambda: as_auth(user)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t"
            ) as c:
                return await c.get(path, params=params)
        finally:
            app.dependency_overrides.clear()

    return call


async def _file(session, book: Book, doc_no: int, page_no: int, **fields) -> Document:
    document = Document(
        user_id=book.user_id,
        file_name=fields.pop("file_name", f"Deed {doc_no}"),
        lifecycle=fields.pop("lifecycle", LIFECYCLE_SIGNED),
        book_id=book.id,
        doc_no=doc_no,
        page_no=page_no,
        **fields,
    )
    session.add(document)
    await session.flush()
    return document


async def test_owner_sees_the_book_and_its_entries(session, http, lawyer, open_book):
    signed = await _file(session, open_book, 1, 1)
    finalized = await _file(
        session,
        open_book,
        2,
        1,
        file_name="Affidavit",
        lifecycle=LIFECYCLE_FINALIZED,
        status=STATUS_COMPLETED,
    )

    r = await http(lawyer, f"/books/{open_book.id}/entries")

    assert r.status_code == 200
    body = r.json()
    assert body["book"]["id"] == str(open_book.id)
    assert body["book"]["entry_count"] == 2
    assert body["entries"] == [
        {
            "id": str(signed.id),
            "file_name": "Deed 1",
            "doc_no": 1,
            "page_no": 1,
            "lifecycle": LIFECYCLE_SIGNED,
            "status": None,
        },
        {
            "id": str(finalized.id),
            "file_name": "Affidavit",
            "doc_no": 2,
            "page_no": 1,
            "lifecycle": LIFECYCLE_FINALIZED,
            "status": STATUS_COMPLETED,
        },
    ]


async def test_closed_book_entries_are_listed(session, http, lawyer, closed_book):
    await _file(session, closed_book, 400, 80)

    r = await http(lawyer, f"/books/{closed_book.id}/entries")

    assert r.status_code == 200
    assert r.json()["book"]["status"] == "CLOSED"
    assert [e["doc_no"] for e in r.json()["entries"]] == [400]


async def test_only_this_books_documents_are_listed(
    session, http, lawyer, open_book, closed_book
):
    mine = await _file(session, open_book, 1, 1)
    await _file(session, closed_book, 1, 1)
    session.add(Document(user_id=lawyer.id, file_name="Unfiled", lifecycle=LIFECYCLE_PREPARING))
    await session.flush()

    r = await http(lawyer, f"/books/{open_book.id}/entries")

    assert [e["id"] for e in r.json()["entries"]] == [str(mine.id)]


async def test_entries_are_ordered_by_register_numbers(session, http, lawyer, closed_book):
    # Filed out of order, and with page order disagreeing with doc order.
    await _file(session, closed_book, 3, 1)
    await _file(session, closed_book, 1, 2)
    await _file(session, closed_book, 2, 1)

    r = await http(lawyer, f"/books/{closed_book.id}/entries")

    assert [(e["doc_no"], e["page_no"]) for e in r.json()["entries"]] == [
        (1, 2),
        (2, 1),
        (3, 1),
    ]


async def test_entries_are_paginated(session, http, lawyer, open_book):
    for n in range(1, 6):
        await _file(session, open_book, n, 1)

    first = await http(lawyer, f"/books/{open_book.id}/entries", limit=2)
    second = await http(lawyer, f"/books/{open_book.id}/entries", limit=2, offset=2)
    past_end = await http(lawyer, f"/books/{open_book.id}/entries", limit=2, offset=10)

    assert [e["doc_no"] for e in first.json()["entries"]] == [1, 2]
    assert [e["doc_no"] for e in second.json()["entries"]] == [3, 4]
    assert past_end.status_code == 200
    assert past_end.json()["entries"] == []
    # The total stays the whole book's, whatever page was asked for.
    assert {r.json()["book"]["entry_count"] for r in (first, second, past_end)} == {5}


async def test_page_size_defaults_to_50(session, http, lawyer, open_book):
    for n in range(1, 52):
        await _file(session, open_book, n, (n - 1) // 5 + 1)

    r = await http(lawyer, f"/books/{open_book.id}/entries")

    assert len(r.json()["entries"]) == 50
    assert r.json()["book"]["entry_count"] == 51


async def test_empty_book_has_no_entries(http, lawyer, open_book):
    r = await http(lawyer, f"/books/{open_book.id}/entries")

    assert r.status_code == 200
    assert r.json()["entries"] == []
    assert r.json()["book"]["entry_count"] == 0


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "many"}],
)
async def test_invalid_pagination_is_refused(http, lawyer, open_book, params):
    r = await http(lawyer, f"/books/{open_book.id}/entries", **params)

    assert r.status_code == 422


async def test_another_lawyers_book_is_not_found(
    session, http, other_lawyer, open_book
):
    await _file(session, open_book, 1, 1)

    r = await http(other_lawyer, f"/books/{open_book.id}/entries")

    assert r.status_code == 404
    assert "entries" not in r.json()


async def test_unknown_book_is_not_found(http, lawyer):
    r = await http(lawyer, f"/books/{uuid.uuid4()}/entries")

    assert r.status_code == 404


async def test_non_lawyer_is_forbidden(http, client, open_book):
    r = await http(client, f"/books/{open_book.id}/entries")

    assert r.status_code == 403


async def test_missing_token_is_unauthorized(session, open_book):
    async def same_session():
        yield session

    app.dependency_overrides[get_db] = same_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            r = await c.get(f"/books/{open_book.id}/entries")
    finally:
        app.dependency_overrides.clear()

    assert r.status_code == 401
