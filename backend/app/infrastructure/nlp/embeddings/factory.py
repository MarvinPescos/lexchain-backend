from app.core import settings
from .base import Embedder
from .mock_embedder import MockEmbedder
from .sbert_embedder import SBERTEmbedder


def build_embedder() -> Embedder:
    if settings.USE_REAL_EMBEDDER:
        return SBERTEmbedder(model_name=settings.SBERT_MODEL)
    return MockEmbedder(dim=384)