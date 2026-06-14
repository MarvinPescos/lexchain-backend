import uuid
from datetime import datetime

from pydantic import BaseModel, Field


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


class BookResponse(BaseModel):
    id: uuid.UUID
    book_number: int
    series_year: int
    document_count: int
    page_count: int
    is_full: bool
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True

