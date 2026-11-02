from pydantic import BaseModel, Field
import uuid
from datetime import datetime
from typing import Literal

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

# Bounds on what the client may send back, so one request cannot blow the
# model's context window.
MAX_HISTORY_TURNS = 10
MAX_TURN_CHARS = 4_000


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=MAX_TURN_CHARS)


class AskRequest(BaseModel):
    question: str
    # The conversation so far, oldest first. The server keeps none of it: the
    # client holds the chat in memory, so it is gone on reload or logout.
    history: list[ChatTurn] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS)


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
