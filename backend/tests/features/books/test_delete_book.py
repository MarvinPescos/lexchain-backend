"""Deleting an empty book cannot race a filing into it."""

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import settings
from app.features.books.constants import BOOK_OPEN
from app.features.books.models import Book
from app.features.books.service import BookService
from app.features.documents.constants import LIFECYCLE_SIGNED
from app.features.documents.models import Document
from app.features.users.models import User
from app.shared.constants import ROLE_LAWYER
from app.shared.errors.exceptions import ConflictError, NotFoundError
from tests.conftest import _make_user


@pytest.fixture
async def committed(connection) -> AsyncGenerator[tuple[async_sessionmaker, User, Book], None]:
    """Two transactions must see each other, so these rows are really committed."""
    engine = create_async_engine(settings.TEST_DB_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        lawyer = await _make_user(db, ROLE_LAWYER, "lawyer")
        book = Book(user_id=lawyer.id, book_number=1, series_year=2026, status=BOOK_OPEN)
        db.add(book)
        await db.commit()
    try:
        yield sessions, lawyer, book
    finally:
        async with sessions() as db:
            await db.execute(delete(Document).where(Document.user_id == lawyer.id))
            await db.execute(delete(Book).where(Book.user_id == lawyer.id))
            await db.execute(delete(User).where(User.id == lawyer.id))
            await db.commit()
        await engine.dispose()


async def _start_filing(db: AsyncSession, lawyer: User, book: Book) -> uuid.UUID:
    """A filing up to, but not including, its commit — the book row stays locked."""
    entry = await BookService(db).claim_entry(lawyer.id, book.id, None, None)
    document = Document(
        user_id=lawyer.id,
        file_name="Deed",
        lifecycle=LIFECYCLE_SIGNED,
        book_id=entry.book_id,
        doc_no=entry.doc_no,
        page_no=entry.page_no,
    )
    db.add(document)
    await db.flush()
    return document.id


async def test_delete_waits_for_a_filing_and_keeps_its_document(committed):
    sessions, lawyer, book = committed

    async with sessions() as filing, sessions() as deleting:
        document_id = await _start_filing(filing, lawyer, book)

        deletion = asyncio.create_task(
            BookService(deleting).delete_book(lawyer.id, book.id)
        )
        await asyncio.sleep(0.3)
        assert not deletion.done(), "delete should wait on the filing's book lock"

        await filing.commit()
        with pytest.raises(ConflictError, match="still holds documents"):
            await deletion

    async with sessions() as db:
        assert await db.scalar(select(Document.id).where(Document.id == document_id))
        assert await db.scalar(select(Book.id).where(Book.id == book.id))


async def test_empty_book_is_deleted(committed):
    sessions, lawyer, book = committed

    async with sessions() as db:
        await BookService(db).delete_book(lawyer.id, book.id)

    async with sessions() as db:
        assert await db.scalar(select(Book.id).where(Book.id == book.id)) is None
        with pytest.raises(NotFoundError):
            await BookService(db).delete_book(lawyer.id, book.id)


async def test_book_with_documents_is_not_deleted(committed):
    sessions, lawyer, book = committed

    async with sessions() as db:
        await _start_filing(db, lawyer, book)
        await db.commit()

    async with sessions() as db:
        with pytest.raises(ConflictError):
            await BookService(db).delete_book(lawyer.id, book.id)
