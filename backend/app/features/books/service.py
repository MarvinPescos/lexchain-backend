import math
import uuid

from app.infrastructure.observability.logging_setup import log
from app.shared.errors.exceptions import BadRequestError, NotFoundError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Book
from .repository import BookRepository
from .schemas import BookCreateRequest, BookResponse

MAX_DOCS_PER_PAGE = 5
MAX_PAGES = 106
MAX_DOCS = MAX_PAGES * MAX_DOCS_PER_PAGE  # 530 documents per book


class BookService:
    """Service layer for book management"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = BookRepository(session)

    # == Read ==

    async def get_all_books(
        self, user_id: uuid.UUID, limit: int = 100, offset: int = 0
    ) -> list[Book]:
        log.info("book.fetch.all", user_id=user_id)
        return await self.repo.get_many(user_id, limit, offset)

    async def get_book(self, user_id: uuid.UUID, book_id: uuid.UUID) -> BookResponse:
        """Return a single book by ID scoped to the user."""
        book = await self.repo.get_one(user_id, book_id)
        if not book:
            raise NotFoundError("Book not found")
        log.info("book.fetch.one", user_id=str(user_id), book_id=str(book_id))
        return BookResponse.model_validate(book)

    # == Write ==

    async def create_book(
        self, user_id: uuid.UUID, data: BookCreateRequest
    ) -> BookResponse:
        """Create a new book for the user with sequence guards."""
        # Guard 1: Duplicate check
        existing_book = await self.repo.get_by_number_and_year(
            user_id, data.book_number, data.series_year
        )
        if existing_book:
            raise BadRequestError(
                f"Book {data.book_number} for the year {data.series_year} already exists."
            )

        # Guard 2: Sequential activation check
        active_book = await self.repo.get_active_book_for_year(user_id, data.series_year)
        if active_book:
            raise BadRequestError(
                f"Cannot create a new book. Book {active_book.book_number} "
                f"for the year {data.series_year} is not full yet."
            )

        book = await self.repo.create(
            user_id=user_id,
            book_number=data.book_number,
            series_year=data.series_year,
            document_count=0,
            page_count=1,
            is_full=False,
        )
        await self.session.commit()
        log.info("book.create", user_id=str(user_id), book_number=data.book_number)
        return BookResponse.model_validate(book)

    async def delete_book(self, user_id: uuid.UUID, book_id: uuid.UUID) -> None:
        """Delete a book. Cascades to all documents via DB constraint."""
        book = await self.repo.get_one(user_id, book_id)
        if not book:
            raise NotFoundError("Book not found")
        await self.repo.delete(book)
        await self.session.commit()
        log.info("book.delete", user_id=str(user_id), book_id=str(book_id))

    # == Page Tracking ==

    async def register_document_upload(
        self, user_id: uuid.UUID, book_id: uuid.UUID
    ) -> int:
        """
        Claim a slot in the book for an incoming document.
        Returns the page_number the document should be assigned to.
        Raises BadRequestError if the book is full (106 pages / 530 docs).
        """
        book = await self.repo.get_one(user_id, book_id)
        if not book:
            raise NotFoundError("Book not found")
        if book.is_full:
            raise BadRequestError(
                "This book is full (106 pages / 530 documents). Please create a new book."
            )

        # Compute page_number BEFORE incrementing
        page_number = (book.document_count // MAX_DOCS_PER_PAGE) + 1

        new_count = book.document_count + 1
        new_page_count = math.ceil(new_count / MAX_DOCS_PER_PAGE)
        is_full = new_count >= MAX_DOCS

        await self.repo.update(
            book,
            document_count=new_count,
            page_count=new_page_count,
            is_full=is_full,
        )

        log.info(
            "book.document.registered",
            book_id=str(book_id),
            page_number=page_number,
            document_count=new_count,
            is_full=is_full,
        )

        return page_number

