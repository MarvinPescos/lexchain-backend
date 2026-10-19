import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.exceptions import IntegrityConstraintError
from app.infrastructure.observability.logging_setup import log
from app.shared.errors.exceptions import BadRequestError, ConflictError, NotFoundError

from .constants import BOOK_CLOSED, BOOK_OPEN, MAX_ENTRIES_PER_PAGE, MAX_PAGES
from .models import UQ_BOOK_NUMBER, UQ_BOOK_ONE_OPEN, Book
from .repository import BookRepository
from .schemas import BookCreateRequest, BookResponse, RegisterEntry


class BookService:
    """Service layer for book management"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = BookRepository(session)

    # == Read ==

    async def get_all_books(
        self, user_id: uuid.UUID, limit: int = 100, offset: int = 0
    ) -> list[BookResponse]:
        # Changed 2026-09-24 13:47 PST — list_for_user, not get_many.
        # get_many has no ORDER BY, so paging through books with limit/offset
        # could skip or repeat one. user_id is also str() now, like every other
        # log line in the service.
        log.info("book.fetch.all", user_id=str(user_id))
        books = await self.repo.list_for_user(user_id, limit, offset)
        # One aggregate query for the whole page, not one per book.
        stats = await self.repo.register_stats([b.id for b in books])
        return [self._to_response(b, stats.get(b.id)) for b in books]

    async def get_book(self, user_id: uuid.UUID, book_id: uuid.UUID) -> BookResponse:
        """Return a single book by ID scoped to the user."""
        book = await self.repo.get_one(user_id, book_id)
        if not book:
            raise NotFoundError("Book not found")
        log.info("book.fetch.one", user_id=str(user_id), book_id=str(book_id))
        stats = await self.repo.register_stats([book.id])
        return self._to_response(book, stats.get(book.id))

    # == Write ==

    async def create_book(
        self, user_id: uuid.UUID, data: BookCreateRequest
    ) -> BookResponse:
        """Create a book. OPEN for current work, CLOSED to migrate a finished one."""
        # Guard 1: Duplicate check
        existing_book = await self.repo.get_by_number_and_year(
            user_id, data.book_number, data.series_year
        )
        if existing_book:
            raise BadRequestError(
                f"Book {data.book_number} for the year {data.series_year} already exists."
            )

        # Guard 2: one register in use per year.
        # Changed 2026-09-24 15:10 PST — only an OPEN book is checked.
        # This used to refuse any new book while another of the same year was
        # not full, which made migrating past work impossible: Book 2 of 2025
        # could never be created because Book 1, freshly created and empty, was
        # "not full". A finished book is created CLOSED and skips the rule.
        if data.status == BOOK_OPEN:
            open_book = await self.repo.get_open_book_for_year(
                user_id, data.series_year
            )
            if open_book:
                raise BadRequestError(
                    f"Cannot open a new book. Book {open_book.book_number} for "
                    f"the year {data.series_year} is still open — close it first, "
                    "or create this book as CLOSED if it is a finished register."
                )

        # Added 2026-09-24 14:30 PST — the database has the final say.
        # Both guards above are check-then-insert, so a request that arrives
        # between another's check and its insert passes them. The unique
        # constraints on Book catch that case; this turns the violation back
        # into the same 400 the guards give instead of a 500. The constraint
        # name in the driver error says which rule was broken.
        try:
            book = await self.repo.create(
                user_id=user_id,
                book_number=data.book_number,
                series_year=data.series_year,
                status=data.status,
                closed_at=(
                    datetime.now(timezone.utc) if data.status == BOOK_CLOSED else None
                ),
            )
        except IntegrityConstraintError as e:
            await self.session.rollback()
            if UQ_BOOK_ONE_OPEN in str(e.original_error):
                raise BadRequestError(
                    f"Cannot open a new book. Another book for the year "
                    f"{data.series_year} is still open."
                )
            if UQ_BOOK_NUMBER in str(e.original_error):
                raise BadRequestError(
                    f"Book {data.book_number} for the year {data.series_year} already exists."
                )
            raise
        await self.session.commit()
        log.info(
            "book.create",
            user_id=str(user_id),
            book_number=data.book_number,
            status=data.status,
        )
        return self._to_response(book, None)

    # Added 2026-09-24 15:10 PST — closing is how the next book gets opened.
    # With the counter gone, "full" is no longer the only way a book ends: a
    # lawyer finishes a register when the paper one is done, whatever the app
    # has seen of it. Closing is one-way — reopening would let two books of a
    # year take new entries again.
    async def close_book(self, user_id: uuid.UUID, book_id: uuid.UUID) -> BookResponse:
        """Close an OPEN book so the next one for its year can be opened."""
        book = await self.repo.get_one(user_id, book_id)
        if not book:
            raise NotFoundError("Book not found")
        if book.status == BOOK_CLOSED:
            raise ConflictError("This book is already closed")

        await self.repo.update(
            book, status=BOOK_CLOSED, closed_at=datetime.now(timezone.utc)
        )
        await self.session.commit()
        # updated_at is set by the database (onupdate=func.now()), which leaves
        # the attribute expired; reading it lazily would do IO outside a greenlet.
        await self.session.refresh(book)
        log.info("book.close", user_id=str(user_id), book_id=str(book_id))

        stats = await self.repo.register_stats([book.id])
        return self._to_response(book, stats.get(book.id))

    async def delete_book(self, user_id: uuid.UUID, book_id: uuid.UUID) -> None:
        """Delete an empty book. A book that holds documents cannot be deleted."""
        try:
            await self._delete_empty_book(user_id, book_id)
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        log.info("book.delete", user_id=str(user_id), book_id=str(book_id))

    async def _delete_empty_book(self, user_id: uuid.UUID, book_id: uuid.UUID) -> None:
        # The same row lock claim_entry takes: a filing in progress finishes
        # first, so the empty check below sees its document and the delete
        # cannot cascade it away.
        book = await self.repo.get_one_for_update(user_id, book_id)
        if not book:
            raise NotFoundError("Book not found")

        # Added 2026-09-24 13:47 PST — refuse to delete a book that holds documents.
        # Deleting cascaded to every document in the book, and so to their
        # extractions, chunks and parties. A finalized document is anchored
        # on-chain and meant to be immutable forever; a register book is where
        # those instruments are entered, so deleting it must not be a way to
        # erase them. In practice the cascade also stopped at document_snapshots
        # (ondelete=RESTRICT) and surfaced as a 500 for any book with a finalized
        # document. Only an empty book — e.g. one created with the wrong number —
        # can be removed now.
        if await self.repo.has_documents(book.id):
            raise ConflictError(
                "This book still holds documents and cannot be deleted. "
                "Only an empty book can be removed."
            )

        await self.repo.delete(book)

    # == Register entries ==
    #
    # Changed 2026-09-24 15:10 PST — entries replace register_document_upload.
    # The book no longer hands out page numbers from an upload counter. Each
    # document carries its own Doc. No. and Page No., as written in its
    # notarial block, and filing validates them against the register:
    #   OPEN book    numbers optional — the next free ones are suggested
    #   CLOSED book  numbers required — copied from the paper register
    # Two steps, because the upload writes to storage in between:
    # check_filing is a lock-free refusal before any bytes are stored;
    # claim_entry is the authoritative check under the book's row lock.

    async def check_filing(
        self,
        user_id: uuid.UUID,
        book_id: uuid.UUID,
        doc_no: int | None,
        page_no: int | None,
    ) -> Book:
        """Refuse a filing that can never succeed, without locking anything."""
        book = await self.repo.get_one(user_id, book_id)
        if not book:
            # Same answer for "no such book" and "someone else's book", so the
            # response does not reveal which book ids exist.
            raise NotFoundError("Book not found")
        self._validate_numbers(book, doc_no, page_no)
        return book

    async def claim_entry(
        self,
        user_id: uuid.UUID,
        book_id: uuid.UUID,
        doc_no: int | None,
        page_no: int | None,
    ) -> RegisterEntry:
        """Reserve a register entry for a document being filed.

        Does not commit: the caller commits together with the document row, so
        the entry exists exactly when the document does. The book's row lock is
        held until that commit, so concurrent filings see each other.
        """
        book = await self.repo.get_one_for_update(user_id, book_id)
        if not book:
            raise NotFoundError("Book not found")
        self._validate_numbers(book, doc_no, page_no)

        if doc_no is None or page_no is None:
            suggested_doc, suggested_page = await self._next_entry(book)
            doc_no = doc_no if doc_no is not None else suggested_doc
            page_no = page_no if page_no is not None else suggested_page

        if await self.repo.is_doc_no_taken(book.id, doc_no):
            raise ConflictError(
                f"Doc. No. {doc_no} is already used in Book {book.book_number}, "
                f"Series of {book.series_year}."
            )

        on_page = await self.repo.count_on_page(book.id, page_no)
        if on_page >= MAX_ENTRIES_PER_PAGE:
            raise ConflictError(
                f"Page {page_no} already holds {MAX_ENTRIES_PER_PAGE} entries."
            )

        # This entry uses up the last page: the register is finished.
        if (
            book.status == BOOK_OPEN
            and page_no == MAX_PAGES
            and on_page + 1 == MAX_ENTRIES_PER_PAGE
        ):
            await self.repo.update(
                book, status=BOOK_CLOSED, closed_at=datetime.now(timezone.utc)
            )
            log.info("book.close.full", book_id=str(book.id))

        log.info(
            "book.entry.claimed",
            book_id=str(book.id),
            doc_no=doc_no,
            page_no=page_no,
        )
        return RegisterEntry(book_id=book.id, doc_no=doc_no, page_no=page_no)

    # ========== helpers =============

    def _validate_numbers(
        self, book: Book, doc_no: int | None, page_no: int | None
    ) -> None:
        if book.status == BOOK_CLOSED and (doc_no is None or page_no is None):
            raise BadRequestError(
                f"Book {book.book_number}, Series of {book.series_year} is closed. "
                "Give the Doc. No. and Page No. from the paper register to file "
                "a document in it."
            )
        if doc_no is not None and doc_no < 1:
            raise BadRequestError("Doc. No. must be 1 or higher")
        if page_no is not None and not (1 <= page_no <= MAX_PAGES):
            raise BadRequestError(f"Page No. must be between 1 and {MAX_PAGES}")

    async def _next_entry(self, book: Book) -> tuple[int, int]:
        """Suggest the next Doc. No. and Page No. after the last entry."""
        _, last_doc, last_page = (await self.repo.register_stats([book.id])).get(
            book.id, (0, None, None)
        )

        doc_no = (last_doc or 0) + 1
        if last_page is None:
            page_no = 1
        elif await self.repo.count_on_page(book.id, last_page) < MAX_ENTRIES_PER_PAGE:
            page_no = last_page
        else:
            page_no = last_page + 1

        if page_no > MAX_PAGES:
            raise BadRequestError(
                f"Book {book.book_number}, Series of {book.series_year} has no "
                f"pages left. Close it and open a new book."
            )
        return doc_no, page_no

    def _to_response(
        self, book: Book, stats: tuple[int, int | None, int | None] | None
    ) -> BookResponse:
        count, last_doc, last_page = stats or (0, None, None)
        return BookResponse(
            id=book.id,
            book_number=book.book_number,
            series_year=book.series_year,
            status=book.status,
            closed_at=book.closed_at,
            entry_count=count,
            last_doc_no=last_doc,
            last_page_no=last_page,
            created_at=book.created_at,
            updated_at=book.updated_at,
        )
