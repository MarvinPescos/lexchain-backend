import uuid

import pytest
from pydantic import ValidationError

from app.features.retrieval.schemas import (
    MAX_HISTORY_TURNS,
    AskRequest,
    ChatTurn,
    SearchHit,
    SearchResponse,
)
from app.features.retrieval.service import RAGService
from app.infrastructure.nlp.llm import LLMResult


class FakeRetrieval:
    def __init__(self):
        self.queries: list[str] = []

    async def search_document(self, user_id, document_id, query, top_k):
        self.queries.append(query)
        return SearchResponse(
            query=query,
            document_id=document_id,
            results=[
                SearchHit(chunk_id=uuid.uuid4(), chunk_index=0, score=0.9, text="June 20, 2026")
            ],
        )


class FakeLLM:
    def __init__(self):
        self.messages = None

    async def generate(self, messages, temperature=0.2, max_tokens=512):
        self.messages = messages
        return LLMResult(answer="ok", model="fake")


HISTORY = [
    ChatTurn(role="user", content="What are the key dates?"),
    ChatTurn(role="assistant", content="June 20, 2026 is when the lease was made."),
]


async def test_history_reaches_the_llm_before_the_new_question():
    retrieval, llm = FakeRetrieval(), FakeLLM()
    await RAGService(retrieval, llm).ask_document(
        uuid.uuid4(), uuid.uuid4(), "can you state it again?", history=HISTORY
    )

    roles = [m.role for m in llm.messages]
    assert roles == ["system", "user", "assistant", "user"]
    assert llm.messages[1].content == "What are the key dates?"
    assert "can you state it again?" in llm.messages[-1].content


async def test_follow_up_is_searched_with_the_previous_question():
    retrieval = FakeRetrieval()
    await RAGService(retrieval, FakeLLM()).ask_document(
        uuid.uuid4(), uuid.uuid4(), "can you state it again?", history=HISTORY
    )

    assert "What are the key dates?" in retrieval.queries[0]


async def test_no_history_behaves_as_before():
    retrieval, llm = FakeRetrieval(), FakeLLM()
    await RAGService(retrieval, llm).ask_document(uuid.uuid4(), uuid.uuid4(), "Who signed?")

    assert retrieval.queries == ["Who signed?"]
    assert [m.role for m in llm.messages] == ["system", "user"]


def test_history_is_bounded():
    turn = {"role": "user", "content": "x"}
    with pytest.raises(ValidationError):
        AskRequest(question="q", history=[turn] * (MAX_HISTORY_TURNS + 1))
    with pytest.raises(ValidationError):
        AskRequest(question="q", history=[{"role": "system", "content": "x"}])
