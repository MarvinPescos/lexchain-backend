import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .constants import BOOK_OPEN


class BookCreateRequest(BaseModel):
    book_number: int = Field(
        ...,
        ge=1,
        le=1000,
        description="The sequential volume number of the physical register.",
    )
    series_year: int = Field(
        ...,
        ge=2000,
        description="The year this book belongs to.",
    )
    # Added 2026-09-24 15:10 PST — lets a finished physical book be migrated.
    # A new book for current work is OPEN. A book whose paper register is
    # already done (past work being brought into the app) is created CLOSED, so
    # it neither blocks nor is blocked by the one-open-book-per-year rule.
    status: Literal["OPEN", "CLOSED"] = Field(
        BOOK_OPEN,
        description=(
            "OPEN for the register in current use. CLOSED to migrate a finished "
            "physical book; documents filed in it must give their Doc. No. and "
            "Page No. from the paper register."
        ),
    )


class BookResponse(BaseModel):
    id: uuid.UUID
    book_number: int
    series_year: int
    status: str = Field(..., description="OPEN | CLOSED")
    closed_at: datetime | None = None
    # Changed 2026-09-24 15:10 PST — counted from the entries, not stored.
    # Replaces document_count / page_count / is_full; see Book for why.
    entry_count: int = Field(0, description="Documents filed in this book")
    last_doc_no: int | None = Field(None, description="Highest Doc. No. used")
    last_page_no: int | None = Field(None, description="Highest Page No. used")
    created_at: datetime
    updated_at: datetime | None = None

    # Changed 2026-09-24 13:47 PST — ConfigDict, not `class Config`.
    # Same reason as AuthenticatedUser: Pydantic deprecated class-based config
    # in 2.0 and removes it in 3.0.
    model_config = ConfigDict(from_attributes=True)


class RegisterEntry(BaseModel):
    """Where a document sits in the notarial register."""

    book_id: uuid.UUID
    doc_no: int
    page_no: int
