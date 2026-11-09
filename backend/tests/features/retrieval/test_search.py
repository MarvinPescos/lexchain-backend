"""Global search hits carry the document details a user recognizes it by."""

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.books.models import Book
from app.features.chunks.models import DocumentChunk
from app.features.documents.constants import LIFECYCLE_PREPARING, LIFECYCLE_SIGNED
from app.features.documents.extraction.models import DocumentExtraction
from app.features.documents.models import Document, DocumentParty
from app.features.retrieval.service import RetrievalService
from app.features.users.models import User
from app.infrastructure.nlp.embeddings import EMBEDDING_DIM, EmbeddingResult
from app.main import app
from app.shared.constants import (
    PARTY_STATUS_ACCEPTED,
    PARTY_STATUS_DECLINED,
    PARTY_STATUS_EXPIRED,
    PARTY_STATUS_PENDING,
)

VECTOR = [1.0] * EMBEDDING_DIM


class FixedEmbedder:
    """Embeds every text to the same vector, so every chunk matches."""

    async def embed(self, text: str) -> EmbeddingResult:
        return EmbeddingResult(vector=VECTOR, dim=EMBEDDING_DIM, meta={})

    async def embed_batch(self, text: list[str]) -> list[EmbeddingResult]:
        return [await self.embed(t) for t in text]


async def _indexed(session: AsyncSession, document: Document) -> Document:
    """Give `document` one embedded chunk, as processing would."""
    session.add(document)
    await session.flush()
    extraction = DocumentExtraction(
        document_id=document.id, engine="mock", text="deed", page_count=1, meta={}
    )
    session.add(extraction)
    await session.flush()
    session.add(
        DocumentChunk(
            document_id=document.id,
            extraction_id=extraction.id,
            chunk_index=0,
            text="deed",
            char_count=4,
            embedding_vec=VECTOR,
            strategy="mock",
            engine="mock",
        )
    )
    await session.flush()
    return document


@pytest.fixture
def retrieval(session: AsyncSession) -> RetrievalService:
    return RetrievalService(session, FixedEmbedder())


async def test_hit_includes_title_date_and_register_reference(
    session: AsyncSession, retrieval: RetrievalService, lawyer: User, open_book: Book
):
    document = await _indexed(
        session,
        Document(
            user_id=lawyer.id,
            file_name="Deed of Sale",
            lifecycle=LIFECYCLE_SIGNED,
            book_id=open_book.id,
            doc_no=45,
            page_no=9,
        ),
    )

    response = await retrieval.search_all(lawyer.id, "deed")

    [hit] = response.results
    assert hit.document_id == document.id
    assert hit.file_name == "Deed of Sale"
    assert hit.created_at == document.created_at
    assert hit.register_reference is not None
    assert hit.register_reference.doc_no == 45
    assert hit.register_reference.page_no == 9
    assert hit.register_reference.book_number == open_book.book_number
    assert hit.register_reference.series_year == open_book.series_year
    assert hit.register_reference.label == "Doc. No. 45, Page No. 9, Book No. 1, Series of 2026"


async def test_unregistered_document_has_no_register_reference(
    session: AsyncSession, retrieval: RetrievalService, lawyer: User
):
    await _indexed(
        session,
        Document(user_id=lawyer.id, file_name="Draft", lifecycle=LIFECYCLE_PREPARING),
    )

    response = await retrieval.search_all(lawyer.id, "deed")

    [hit] = response.results
    assert hit.file_name == "Draft"
    assert hit.register_reference is None


async def test_other_users_documents_are_not_returned(
    session: AsyncSession, retrieval: RetrievalService, lawyer: User, other_lawyer: User
):
    await _indexed(
        session,
        Document(user_id=other_lawyer.id, file_name="Theirs", lifecycle=LIFECYCLE_PREPARING),
    )

    response = await retrieval.search_all(lawyer.id, "deed")

    assert response.results == []


async def _shared_with(
    session: AsyncSession, owner: User, party: User, status: str
) -> Document:
    """An indexed document `owner` invited `party` to, in `status`."""
    document = await _indexed(
        session,
        Document(user_id=owner.id, file_name="Shared", lifecycle=LIFECYCLE_PREPARING),
    )
    session.add(
        DocumentParty(
            document_id=document.id, user_id=party.id, email=party.email, status=status
        )
    )
    await session.flush()
    return document


async def test_accepted_party_finds_the_shared_document(
    session: AsyncSession, retrieval: RetrievalService, lawyer: User, client: User
):
    document = await _shared_with(session, lawyer, client, PARTY_STATUS_ACCEPTED)

    response = await retrieval.search_all(client.id, "deed")

    [hit] = response.results
    assert hit.document_id == document.id


async def test_lawyer_party_finds_another_lawyers_shared_document(
    session: AsyncSession,
    retrieval: RetrievalService,
    lawyer: User,
    other_lawyer: User,
):
    document = await _shared_with(session, other_lawyer, lawyer, PARTY_STATUS_ACCEPTED)

    response = await retrieval.search_all(lawyer.id, "deed")

    [hit] = response.results
    assert hit.document_id == document.id


@pytest.mark.parametrize(
    "status", [PARTY_STATUS_PENDING, PARTY_STATUS_DECLINED, PARTY_STATUS_EXPIRED]
)
async def test_party_without_access_finds_nothing(
    session: AsyncSession,
    retrieval: RetrievalService,
    lawyer: User,
    client: User,
    status: str,
):
    await _shared_with(session, lawyer, client, status)

    response = await retrieval.search_all(client.id, "deed")

    assert response.results == []


async def test_openapi_documents_the_hit_details():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as http:
        spec = (await http.get("/openapi.json")).json()

    schemas = spec["components"]["schemas"]
    hit = schemas["GlobalSearchHit"]
    assert {"file_name", "created_at", "register_reference"} <= hit["properties"].keys()
    assert {"file_name", "created_at"} <= set(hit["required"])
    assert set(schemas["RegisterReference"]["required"]) == {
        "doc_no", "page_no", "book_number", "series_year", "label"
    }
