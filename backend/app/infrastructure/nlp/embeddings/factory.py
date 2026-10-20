from app.core import settings

from .base import EMBEDDING_DIM, Embedder
from .mock_embedder import MockEmbedder


def build_embedder() -> Embedder:
    if not settings.USE_REAL_EMBEDDER:
        return MockEmbedder(dim=EMBEDDING_DIM)

    if settings.EMBEDDER_ENGINE == "e5":
        from .e5_embedder import E5Embedder

        embedder = E5Embedder(model_name=settings.E5_MODEL)
    elif settings.EMBEDDER_ENGINE == "sbert":
        from .sbert_embedder import SBERTEmbedder

        embedder = SBERTEmbedder(model_name=settings.SBERT_MODEL)
    else:
        raise ValueError(f"Unsupported EMBEDDER_ENGINE: {settings.EMBEDDER_ENGINE}")

    # A vector of the wrong width only fails at insert time, deep in the
    # worker; refuse the model up front instead.
    if embedder.dim != EMBEDDING_DIM:
        raise ValueError(
            f"{settings.EMBEDDER_ENGINE} model produces {embedder.dim}-dim vectors; "
            f"document_chunks.embedding_vec is {EMBEDDING_DIM}"
        )
    return embedder
