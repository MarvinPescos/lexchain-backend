from pydantic import BaseModel, Field, computed_field
import uuid
from datetime import datetime


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)


class SearchHit(BaseModel):
    chunk_id: uuid.UUID
    chunk_index: int
    score: float
    text: str


class SearchResponse(BaseModel):
    query: str
    document_id: uuid.UUID
    results: list[SearchHit]

class AskRequest(BaseModel):
    question: str


class RegisterReference(BaseModel):
    """Where a document sits in the notarial register, with its book's numbers."""

    doc_no: int = Field(..., description="Doc. No. in the register")
    page_no: int = Field(..., description="Page No. in the register")
    book_number: int = Field(..., description="Book No. of the register book")
    series_year: int = Field(..., description="Series year of the register book")

    @computed_field(description="The entry as written in the notarial block")  # type: ignore[prop-decorator]
    @property
    def label(self) -> str:
        return (
            f"Doc. No. {self.doc_no}, Page No. {self.page_no}, "
            f"Book No. {self.book_number}, Series of {self.series_year}"
        )


class GlobalSearchHit(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    chunk_index: int
    score: float
    file_name: str = Field(..., description="Display name of the document")
    created_at: datetime = Field(..., description="When the document was created")
    register_reference: RegisterReference | None = Field(
        None, description="Where the document is filed; null if not in a register"
    )


class GlobalSearchResponse(BaseModel):
    query: str
    results: list[GlobalSearchHit]
