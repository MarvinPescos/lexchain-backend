import asyncio
import hashlib
import random

from .base import EmbeddingResult


class MockEmbedder:
    """
    Deterministic mock embedder.

    Returns a seeded pseudo-random float vector so tests are reproducible.
    Replace with SBERT / OpenAI later — same interface.
    """

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    async def embed(self, text: str) -> EmbeddingResult:
        await asyncio.sleep(0.05)  # simulate latency
        seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16) % (2**32)
        rng = random.Random(seed)
        vector = [rng.uniform(-1.0, 1.0) for _ in range(self.dim)]
        return EmbeddingResult(
            vector=vector,
            dim=self.dim,
            meta={"engine": "mock", "engine_version": "0.1"},
        )

    async def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]:
        return [await self.embed(t) for t in texts]