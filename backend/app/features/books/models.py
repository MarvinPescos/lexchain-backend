from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database import TimestampMixin, UserOwned

from .constants import BOOK_OPEN

if TYPE_CHECKING:
    from app.features.documents import Document
    from app.features.users import User


# Constraint names are referenced by BookService.create_book to tell the two
# violations apart, so rename them in both places.
UQ_BOOK_NUMBER = "uq_book_user_number_year"
UQ_BOOK_ONE_OPEN = "uq_book_one_open_per_year"


# Changed 2026-09-24 15:10 PST — a book is a register, not a counter.
# The book used to count its own uploads (document_count, page_count, is_full)
# and hand out page numbers from that count. That only works if every entry
# ever written in the physical book is uploaded, in order — false for a lawyer
# migrating past work, where Book 2 of 2025 may have 400 real entries and 20
# uploads. The register numbers now live on each document (doc_no, page_no, as
# written in the notarial block), and everything the book used to store is
# counted from them. `is_full` became a status: a full book is closed.
class Book(UserOwned, TimestampMixin):
    __tablename__ = "books"

    # Added 2026-09-24 14:30 PST — enforce the two book rules in the database.
    # create_book checked both in Python (check, then insert), so two requests
    # arriving together both passed the check and both inserted: a duplicate
    # "Book 1, Series of 2026", or two open books for one year. The checks stay
    # for their clear error messages; these are what actually guarantee it.
    #  - one row per (owner, book number, series year)
    #  - at most one OPEN book per owner per year — partial unique index, so
    #    any number of closed books of the same year can coexist.
    __table_args__ = (
        UniqueConstraint(
            "user_id", "book_number", "series_year", name=UQ_BOOK_NUMBER
        ),
        Index(
            UQ_BOOK_ONE_OPEN,
            "user_id",
            "series_year",
            unique=True,
            postgresql_where=text(f"status = '{BOOK_OPEN}'"),
        ),
    )

    # id comes from Base, user_id from UserOwned.
    book_number: Mapped[int] = mapped_column(Integer, nullable=False)
    series_year: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BOOK_OPEN, server_default=BOOK_OPEN
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # === relationship ===
    user: Mapped["User"] = relationship(back_populates="books")
    documents: Mapped[list["Document"]] = relationship(
        back_populates="book", cascade="all, delete-orphan"
    )
