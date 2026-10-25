import uuid
from typing import TYPE_CHECKING

from sqlalchemy import exists, func, select

from app.infrastructure.database import UserScopeRepository

from .constants import BOOK_OPEN
from .models import Book

if TYPE_CHECKING:
    from app.features.documents.models import Document


class BookRepository(UserScopeRepository[Book]):
    """Repository pattern for Book"""

    def __init__(self, session):
        super().__init__(Book, session)

    # === Custom methods ===

    # Added 2026-09-24 13:47 PST — a stable order for the book list.
    # get_many() from UserScopeRepository has no ORDER BY, so Postgres returned
    # rows in whatever order they sat on disk and limit/offset pages could skip
    # or repeat a book between requests. Newest series first, then by volume,
    # matches how the physical registers are shelved.
    async def list_for_user(
        self, user_id: uuid.UUID, limit: int = 100, offset: int = 0
    ) -> list[Book]:
        """Books owned by this user, newest series first."""
        stmt = (
            select(Book)
            .where(Book.user_id == user_id)
            .order_by(Book.series_year.desc(), Book.book_number.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # Added 2026-09-24 13:47 PST — lock the book while an entry is filed in it.
    # Changed 2026-09-24 15:10 PST — now guards entry numbers, not a counter.
    # Filing reads the register (last doc no., entries on a page) and then
    # writes a new entry. Two uploads into the same book at once would both be
    # suggested the same Doc. No. and both see room on the same page. FOR
    # UPDATE makes the second filing wait for the first to commit.
    async def get_one_for_update(
        self, user_id: uuid.UUID, book_id: uuid.UUID
    ) -> Book | None:
        """Fetch an owned book and hold a row lock until the transaction ends."""
        stmt = (
            select(Book)
            .where(Book.user_id == user_id, Book.id == book_id)
            .with_for_update()
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    # Added 2026-09-24 13:47 PST — supports the delete guard in BookService.
    async def has_documents(self, book_id: uuid.UUID) -> bool:
        """Whether any document is filed in this book."""
        from app.features.documents.models import Document

        result = await self.session.execute(
            select(exists().where(Document.book_id == book_id))
        )
        return bool(result.scalar())

    async def get_by_number_and_year(
        self, user_id: uuid.UUID, book_number: int, series_year: int
    ) -> Book | None:
        """Find a book by number and year scoped to the user."""
        stmt = select(Book).where(
            Book.user_id == user_id,
            Book.book_number == book_number,
            Book.series_year == series_year,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_open_book_for_year(
        self, user_id: uuid.UUID, series_year: int
    ) -> Book | None:
        """The OPEN book for a year, if any (there is at most one)."""
        stmt = select(Book).where(
            Book.user_id == user_id,
            Book.series_year == series_year,
            Book.status == BOOK_OPEN,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    # === Register entries ===
    #
    # Added 2026-09-24 15:10 PST — the register is read from the documents.
    # An entry is any document filed in the book — a document is filed only
    # once its signed copy is attached, so book_id set means an entry exists.

    async def register_stats(
        self, book_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[int, int | None, int | None]]:
        """(entry_count, last_doc_no, last_page_no) per book, in one query."""
        from app.features.documents.models import Document

        if not book_ids:
            return {}

        result = await self.session.execute(
            select(
                Document.book_id,
                func.count(),
                func.max(Document.doc_no),
                func.max(Document.page_no),
            )
            .where(Document.book_id.in_(book_ids))
            .group_by(Document.book_id)
        )
        return {row[0]: (row[1], row[2], row[3]) for row in result.all()}

    # Added 2026-10-25 20:39 PST — the register list for one book, a page at a time.
    # Doc. No. is unique per book (UQ_DOCUMENT_REGISTER_ENTRY), so document id
    # never decides the order today; it keeps paging stable if that ever changes.
    async def list_entries(
        self, book_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> list["Document"]:
        """Documents filed in this book, in register order."""
        from app.features.documents.models import Document

        result = await self.session.execute(
            select(Document)
            .where(Document.book_id == book_id)
            .order_by(Document.doc_no, Document.page_no, Document.id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def is_doc_no_taken(self, book_id: uuid.UUID, doc_no: int) -> bool:
        from app.features.documents.models import Document

        result = await self.session.execute(
            select(
                exists().where(
                    Document.book_id == book_id,
                    Document.doc_no == doc_no,
                )
            )
        )
        return bool(result.scalar())

    async def count_on_page(self, book_id: uuid.UUID, page_no: int) -> int:
        from app.features.documents.models import Document

        result = await self.session.execute(
            select(func.count()).where(
                Document.book_id == book_id,
                Document.page_no == page_no,
            )
        )
        return result.scalar() or 0
