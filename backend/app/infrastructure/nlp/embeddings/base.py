from typing import Protocol

from pydantic import BaseModel

# Width of document_chunks.embedding_vec. Every embedder the factory builds
# must produce vectors of this size; changing it needs a migration.
EMBEDDING_DIM = 768


class EmbeddingResult(BaseModel):
    vector: list[float]
    dim: int
    meta: dict | None = None


class Embedder(Protocol):
    # `embed` is for a search query, `embed_batch` for the document passages
    # being indexed. Asymmetric models (e5) encode the two differently.
    async def embed(self, text: str) -> EmbeddingResult: ...

    async def embed_batch(self, text: list[str]) -> list[EmbeddingResult]: ...
