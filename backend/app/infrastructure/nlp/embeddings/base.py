from typing import Protocol
from pydantic import BaseModel

class EmbeddingResult(BaseModel):
    vector: list[float]
    dim: int
    meta: dict | None = None

class Embedder(Protocol):
    async def embed(self, text: str) -> EmbeddingResult: 
        ...

    async def embed_batch(self, text: list[str]) -> list[EmbeddingResult]:
        ...


