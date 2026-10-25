from pydantic import BaseModel, Field
import uuid
from datetime import datetime

from app.features.documents.schemas import RegisterReference


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
