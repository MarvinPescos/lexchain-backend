from typing import Protocol
from pydantic import BaseModel

class Chunk(BaseModel):
    index: int
    text: str
    char_count: int
    meta: dict | None = None

class ChunkResult(BaseModel):
    chunks: list[Chunk]
    strategy: str
    meta: dict | None = None

class Chunker(Protocol):
    async def split(self, text: str) -> ChunkResult:
        ...
