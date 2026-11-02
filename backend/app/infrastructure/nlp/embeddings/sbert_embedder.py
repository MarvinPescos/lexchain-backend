import asyncio

from sentence_transformers import SentenceTransformer

from .base import EmbeddingResult


class SBERTEmbedder:
    """
    Local SBERT embedder using sentence-transformers

    Model is loaded once per process. Inference runs in a thread pool
    because the library is sync and CPU-bound
    """

    _model: SentenceTransformer | None = None
    _model_name: str = ""

    def __init__(self, model_name: str = "all-MiniLm-L6-v2") -> None:
        self.model_name = model_name
        self._load_model()
        assert SBERTEmbedder._model is not None
        dim = SBERTEmbedder._model.get_embedding_dimension()
        if dim is None:
            raise ValueError(f"Model {model_name!r} has no embedding dimension")
        self.dim = dim

    # run only once.
    def _load_model(self) -> None:
        if SBERTEmbedder._model is None or SBERTEmbedder._model_name != self.model_name:
            SBERTEmbedder._model = SentenceTransformer(self.model_name)
            SBERTEmbedder._model_name = self.model_name

    # helpers to (get called in embed and embed_batch).
    def _encode(self, text: list[str]) -> list[list[float]]:
        assert SBERTEmbedder._model is not None
        vectors = SBERTEmbedder._model.encode(
            text,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vectors.tolist()

    # Basically this one is like per document??
    async def embed(self, text: str) -> EmbeddingResult:
        vector = (await asyncio.to_thread(self._encode, [text]))[0]
        return EmbeddingResult(
            vector=vector,
            dim=self.dim,
            meta={
                "engine": "sbert",
                "engine_version": self.model_name,
                "normalized": True,
            },
        )

    async def embed_batch(self, text: list[str]) -> list[EmbeddingResult]:
        if not text:
            return []
        vectors = await asyncio.to_thread(self._encode, text)
        return [
            EmbeddingResult(
                vector=v,
                dim=self.dim,
                meta={
                    "engine": "sbert",
                    "engine_version": self.model_name,
                    "normalized": True,
                },
            )
            for v in vectors
        ]
