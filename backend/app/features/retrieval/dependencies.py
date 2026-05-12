from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import get_db
from app.infrastructure.nlp.embeddings import Embedder, build_embedder
from app.infrastructure.nlp.llm.groq_llm import GroqLLMProvider
from app.infrastructure.nlp.llm.mock_llm import MockLLMProvider
from app.core import settings

from .service import RetrievalService, RAGService


def get_embedder() -> Embedder:
    return build_embedder()

def get_retrieval_service(
    db: AsyncSession = Depends(get_db),
    embedder: Embedder = Depends(get_embedder),
) -> RetrievalService:
    return RetrievalService(db, embedder)

def get_llm():
    use_real = settings.USE_REAL_LLM
    return GroqLLMProvider() if use_real else MockLLMProvider()

def get_rag_service(
    retrieval: RetrievalService = Depends(get_retrieval_service),
    llm = Depends(get_llm),
) -> RAGService:
    return RAGService(retrieval, llm)