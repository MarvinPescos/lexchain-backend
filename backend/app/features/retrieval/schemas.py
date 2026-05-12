from pydantic import BaseModel, Field
import uuid


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
    
class GlobalSearchResponse(BaseModel):
    query: str
    results: list[GlobalSearchHit]