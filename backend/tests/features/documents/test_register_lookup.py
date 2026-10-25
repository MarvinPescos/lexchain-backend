"""GET /documents/register-lookup — find a document by its full register reference."""

from datetime import datetime

import httpx
import pytest

from app.features.books.constants import BOOK_OPEN
from app.features.books.models import Book
from app.features.documents.constants import LIFECYCLE_FINALIZED, LIFECYCLE_SIGNED
from app.features.documents.models import Document, DocumentParty
from app.infrastructure.database import get_db
from app.infrastructure.security.dependencies import get_current_user
from app.main import app
from app.shared.constants import (
    PARTY_STATUS_ACCEPTED,
    PARTY_STATUS_DECLINED,
    PARTY_STATUS_PENDING,
)
from tests.conftest import as_auth

PATH = "/documents/register-lookup"


@pytest.fixture
def http(session):
    """Call the API as a given user (or anonymously), inside the test's transaction."""

    async def same_session():
        yield session

    async def call(user, **params):
        app.dependency_overrides[get_db] = same_session
        if user is not None:
            app.dependency_overrides[get_current_user] = lambda: as_auth(user)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t"
            ) as c:
                return await c.get(PATH, params=params)
        finally:
            app.dependency_overrides.clear()

    return call


def _ref(book: Book, doc_no: int, page_no: int) -> dict:
    return {
        "doc_no": doc_no,
        "page_no": page_no,
        "book_number": book.book_number,
        "series_year": book.series_year,
    }


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


async def _invite(session, document: Document, user, status: str) -> None:
    session.add(
        DocumentParty(
            document_id=document.id, user_id=user.id, email=user.email, status=status
        )
    )
    await session.flush()


async def test_complete_reference_finds_the_document(session, http, lawyer, open_book):
    document = await _file(session, open_book, 25, 10, file_name="Deed of Sale")

    r = await http(lawyer, **_ref(open_book, 25, 10))

    assert r.status_code == 200
    [hit] = r.json()["results"]
    assert datetime.fromisoformat(hit.pop("created_at")) == document.created_at
    assert hit == {
            "document_id": str(document.id),
            "file_name": "Deed of Sale",
            "register_reference": {
                "doc_no": 25,
                "page_no": 10,
                "book_number": 1,
                "series_year": 2026,
                "label": "Doc. No. 25, Page No. 10, Book No. 1, Series of 2026",
            },
    }


async def test_closed_book_record_without_processing_is_found(
    session, http, lawyer, closed_book
):
    # Filed from the paper register: no OCR, no chunks, never finalized.
    document = await _file(session, closed_book, 400, 80)

    r = await http(lawyer, **_ref(closed_book, 400, 80))

    assert [h["document_id"] for h in r.json()["results"]] == [str(document.id)]


async def test_finalized_record_is_found(session, http, lawyer, open_book):
    document = await _file(session, open_book, 3, 1, lifecycle=LIFECYCLE_FINALIZED)

    r = await http(lawyer, **_ref(open_book, 3, 1))

    assert [h["document_id"] for h in r.json()["results"]] == [str(document.id)]


async def test_record_past_the_first_list_page_is_found(session, http, lawyer, open_book):
    for n in range(1, 61):
        await _file(session, open_book, n, (n - 1) // 5 + 1)

    # The oldest record sits beyond the list's default page of 50.
    r = await http(lawyer, **_ref(open_book, 1, 1))

    assert [h["register_reference"]["doc_no"] for h in r.json()["results"]] == [1]


@pytest.mark.parametrize(
    "field, wrong",
    [("doc_no", 26), ("page_no", 11), ("book_number", 9), ("series_year", 2025)],
)
async def test_any_mismatched_field_finds_nothing(
    session, http, lawyer, open_book, field, wrong
):
    await _file(session, open_book, 25, 10)

    r = await http(lawyer, **{**_ref(open_book, 25, 10), field: wrong})

    assert r.status_code == 200
    assert r.json()["results"] == []


async def test_unknown_reference_finds_nothing(http, lawyer):
    r = await http(lawyer, doc_no=1, page_no=1, book_number=1, series_year=2026)

    assert r.status_code == 200
    assert r.json() == {"results": []}


@pytest.mark.parametrize("missing", ["doc_no", "page_no", "book_number", "series_year"])
async def test_every_field_is_required(http, lawyer, missing):
    params = {"doc_no": 1, "page_no": 1, "book_number": 1, "series_year": 2026}
    del params[missing]

    r = await http(lawyer, **params)

    assert r.status_code == 422


@pytest.mark.parametrize(
    "field, value",
    [
        ("doc_no", 0),
        ("doc_no", "one"),
        ("page_no", 0),
        ("page_no", -3),
        ("page_no", "1.5"),
        ("book_number", 0),
        ("book_number", 1001),
        ("series_year", 1999),
        ("series_year", "twenty"),
    ],
)
async def test_out_of_range_or_non_integer_values_are_refused(http, lawyer, field, value):
    params = {"doc_no": 1, "page_no": 1, "book_number": 1, "series_year": 2026}

    r = await http(lawyer, **{**params, field: value})

    assert r.status_code == 422


async def test_accepted_party_finds_the_document(session, http, lawyer, client, open_book):
    document = await _file(session, open_book, 25, 10)
    await _invite(session, document, client, PARTY_STATUS_ACCEPTED)

    r = await http(client, **_ref(open_book, 25, 10))

    assert [h["document_id"] for h in r.json()["results"]] == [str(document.id)]


@pytest.mark.parametrize("status", [PARTY_STATUS_PENDING, PARTY_STATUS_DECLINED])
async def test_unanswered_or_declined_invitee_finds_nothing(
    session, http, lawyer, client, open_book, status
):
    document = await _file(session, open_book, 25, 10)
    await _invite(session, document, client, status)

    r = await http(client, **_ref(open_book, 25, 10))

    assert r.json()["results"] == []


async def test_revoked_party_finds_nothing(
    session, http, sharing, lawyer, client, open_book
):
    document = await _file(session, open_book, 25, 10)
    await _invite(session, document, client, PARTY_STATUS_ACCEPTED)
    await sharing.remove_party(as_auth(lawyer), document.id, client.id)

    r = await http(client, **_ref(open_book, 25, 10))

    assert r.json()["results"] == []


async def test_unrelated_user_finds_nothing(session, http, other_lawyer, open_book):
    await _file(session, open_book, 25, 10)

    r = await http(other_lawyer, **_ref(open_book, 25, 10))

    assert r.json()["results"] == []


async def test_same_reference_under_another_owner_stays_private(
    session, http, lawyer, other_lawyer, open_book
):
    mine = await _file(session, open_book, 25, 10)
    their_book = Book(
        user_id=other_lawyer.id,
        book_number=open_book.book_number,
        series_year=open_book.series_year,
        status=BOOK_OPEN,
    )
    session.add(their_book)
    await session.flush()
    theirs = await _file(session, their_book, 25, 10)

    mine_r = await http(lawyer, **_ref(open_book, 25, 10))
    theirs_r = await http(other_lawyer, **_ref(open_book, 25, 10))

    assert [h["document_id"] for h in mine_r.json()["results"]] == [str(mine.id)]
    assert [h["document_id"] for h in theirs_r.json()["results"]] == [str(theirs.id)]


async def test_every_accessible_match_is_returned_once(
    session, http, lawyer, other_lawyer, open_book
):
    # The same reference in another lawyer's register, shared with this lawyer.
    mine = await _file(session, open_book, 25, 10)
    their_book = Book(
        user_id=other_lawyer.id,
        book_number=open_book.book_number,
        series_year=open_book.series_year,
        status=BOOK_OPEN,
    )
    session.add(their_book)
    await session.flush()
    shared = await _file(session, their_book, 25, 10)
    await _invite(session, shared, lawyer, PARTY_STATUS_ACCEPTED)

    r = await http(lawyer, **_ref(open_book, 25, 10))

    ids = [h["document_id"] for h in r.json()["results"]]
    assert sorted(ids) == sorted([str(mine.id), str(shared.id)])


async def test_missing_token_is_unauthorized(http, open_book):
    r = await http(None, **_ref(open_book, 25, 10))

    assert r.status_code == 401


async def test_openapi_documents_the_lookup():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        spec = (await c.get("/openapi.json")).json()

    op = spec["paths"][PATH]["get"]
    params = {p["name"]: p for p in op["parameters"]}
    assert set(params) == {"doc_no", "page_no", "book_number", "series_year"}
    assert all(p["required"] for p in params.values())
    assert {"200", "401", "422"} <= op["responses"].keys()
    hit = spec["components"]["schemas"]["RegisterLookupHit"]
    assert set(hit["required"]) == {
        "document_id", "file_name", "created_at", "register_reference"
    }
