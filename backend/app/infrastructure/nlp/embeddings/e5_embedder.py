import asyncio

from sentence_transformers import SentenceTransformer

from .base import EmbeddingResult


class E5Embedder:
    """
    Local e5 embedder (intfloat/e5-base-v2 by default) via sentence-transformers.

    e5 was trained with role prefixes: queries are encoded as "query: ..."
    and indexed text as "passage: ...". Leaving them out measurably hurts
    retrieval, so the prefix is applied here rather than by callers.

    Model is loaded once per process; inference runs in a thread pool because
    the library is sync and CPU-bound.
    """

    QUERY_PREFIX = "query: "
    PASSAGE_PREFIX = "passage: "

    _model: SentenceTransformer | None = None
    _model_name: str = ""

    def __init__(self, model_name: str = "intfloat/e5-base-v2") -> None:
        self.model_name = model_name
        self._load_model()
        assert E5Embedder._model is not None
        dim = E5Embedder._model.get_embedding_dimension()
        if dim is None:
            raise ValueError(f"Model {model_name!r} has no embedding dimension")
        self.dim = dim

    def _load_model(self) -> None:
        if E5Embedder._model is None or E5Embedder._model_name != self.model_name:
            E5Embedder._model = SentenceTransformer(self.model_name)
            E5Embedder._model_name = self.model_name

    def _encode(self, text: list[str]) -> list[list[float]]:
        assert E5Embedder._model is not None
        vectors = E5Embedder._model.encode(
            text,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vectors.tolist()

    def _result(self, vector: list[float], role: str) -> EmbeddingResult:
        return EmbeddingResult(
            vector=vector,
            dim=self.dim,
            meta={
                "engine": "e5",
                "engine_version": self.model_name,
                "normalized": True,
                "role": role,
            },
        )

    async def embed(self, text: str) -> EmbeddingResult:
        vector = (await asyncio.to_thread(self._encode, [self.QUERY_PREFIX + text]))[0]
        return self._result(vector, "query")

    async def embed_batch(self, text: list[str]) -> list[EmbeddingResult]:
        if not text:
            return []
        vectors = await asyncio.to_thread(
            self._encode, [self.PASSAGE_PREFIX + t for t in text]
        )
        return [self._result(v, "passage") for v in vectors]
