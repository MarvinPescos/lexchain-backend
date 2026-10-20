"""e5 embedder: role prefixes, and the factory's dimension guard."""

import numpy as np
import pytest

from app.core import settings
from app.infrastructure.nlp.embeddings import EMBEDDING_DIM, build_embedder, e5_embedder
from app.infrastructure.nlp.embeddings.e5_embedder import E5Embedder


class FakeModel:
    def __init__(self, name: str, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim
        self.seen: list[str] = []

    def get_embedding_dimension(self) -> int:
        return self.dim

    def encode(self, texts, **kwargs):
        self.seen.extend(texts)
        return np.zeros((len(texts), self.dim))


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch):
    holder: dict = {}

    def load(name):
        holder["model"] = FakeModel(name, holder.get("dim", EMBEDDING_DIM))
        return holder["model"]

    monkeypatch.setattr(e5_embedder, "SentenceTransformer", load)
    monkeypatch.setattr(E5Embedder, "_model", None)
    return holder


async def test_queries_and_passages_get_their_prefixes(model):
    embedder = E5Embedder("intfloat/e5-base-v2")

    query = await embedder.embed("who is the vendee?")
    passages = await embedder.embed_batch(["The vendee is Juan.", "Sold for 1M."])

    assert model["model"].seen == [
        "query: who is the vendee?",
        "passage: The vendee is Juan.",
        "passage: Sold for 1M.",
    ]
    assert query.dim == EMBEDDING_DIM
    assert [p.meta["role"] for p in passages] == ["passage", "passage"]
    assert query.meta["engine_version"] == "intfloat/e5-base-v2"


async def test_empty_batch_skips_the_model(model):
    assert await E5Embedder().embed_batch([]) == []
    assert model["model"].seen == []


def test_factory_builds_e5(monkeypatch, model):
    monkeypatch.setattr(settings, "USE_REAL_EMBEDDER", True)
    monkeypatch.setattr(settings, "EMBEDDER_ENGINE", "e5")
    assert isinstance(build_embedder(), E5Embedder)


def test_factory_refuses_a_model_of_the_wrong_width(monkeypatch, model):
    monkeypatch.setattr(settings, "USE_REAL_EMBEDDER", True)
    monkeypatch.setattr(settings, "EMBEDDER_ENGINE", "e5")
    model["dim"] = 384
    with pytest.raises(ValueError, match="384-dim"):
        build_embedder()


def test_mock_matches_the_column(monkeypatch):
    monkeypatch.setattr(settings, "USE_REAL_EMBEDDER", False)
    assert build_embedder().dim == EMBEDDING_DIM
