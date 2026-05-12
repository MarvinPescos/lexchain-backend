import math
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.nlp.embeddings import Embedder
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.nlp.llm import LLMProvider, LLMMessage
from app.shared.errors.exceptions import NotFoundError

from app.features.documents.repository import DocumentRepository
from app.features.chunks.repository import DocumentChunkRepository

from .schemas import GlobalSearchHit, GlobalSearchResponse,SearchHit, SearchResponse


class RetrievalService:
    def __init__(self, session: AsyncSession, embedder: Embedder) -> None:
        self.session = session
        self.embedder = embedder
        self.doc_repo = DocumentRepository(session)
        self.chunk_repo = DocumentChunkRepository(session)

    async def search_document(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        query: str,
        top_k: int = 5
    ) -> SearchResponse:

        doc = await self.doc_repo.get_one(user_id, document_id)
        
        if not doc:
            raise NotFoundError("Document not found")
        
        q_embed = await self.embedder.embed(query)
        top = await self.chunk_repo.search_document(document_id, q_embed.vector, top_k)

        log.info(
            "retrieval.search",
            document_id=str(document_id),
            user_id=str(user_id),
            query_len=len(query),
            returned=len(top),
        )

        return SearchResponse(
            query=query,
            document_id=document_id,
            results=[
                SearchHit(
                    chunk_id=c.id,
                    chunk_index=c.chunk_index,
                    score=float(score),
                    text=c.text,
                )
                for c, score in top
            ]
        )

    
    async def search_all(
        self, 
        user_id: uuid.UUID,
        query: str, # basta something naa sa docs
        top_k: int = 3
    ) -> GlobalSearchResponse: 

        q_embed = await self.embedder.embed(query)
        top = await self.chunk_repo.search_user(user_id, q_embed.vector, top_k)

        log.info(
            "retrieval.search.global",
            user_id=str(user_id),
            query_len=len(query),
            returned=len(top)
        )

        return GlobalSearchResponse(
            query=query,
            results=[
                GlobalSearchHit(
                    chunk_id=c.id,
                    document_id=c.document_id,
                    chunk_index=c.chunk_index,
                    score=float(score),
                )
                for c, score in top
            ],
        )

class RAGService:
    def __init__(self, retrieval: RetrievalService, llm: LLMProvider) -> None:
        self.retrieval = retrieval
        self.llm = llm

    async def ask_document(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        question: str,
        top_k: int = 2,
    ):
        hits = await self.retrieval.search_document(user_id, document_id, question, top_k)
        context = "\n\n".join(f"[chunk {h.chunk_index}]\n{h.text}" for h in hits.results)
        return await self._answer(question, context, hits.results, scoped_to_document=True)


    # async def ask_all(
    #     self,
    #     user_id: uuid.UUID,
    #     question: str,
    #     top_k: int = 5,
    # ):
    #     hits = await self.retrieval.search_all(user_id, question, top_k)
    #     context = "\n\n".join(
    #         f"[doc {h.document_id} | chunk {h.chunk_index}]\n{h.text}"
    #         for h in hits.results
    #     )
    #     return await self._answer(question, context, hits.results, scoped_to_document=False)

        
    async def _answer(self, question, context, hits, scoped_to_document: bool):
        scope_note = (
            "You are a legal document assistant. Answer ONLY using the provided context."
            if scoped_to_document
            else "You are a legal document assistant answering across the user's entire document library. "
                 "Answer ONLY using the provided context."
        )
        system_prompt = (
            f"{scope_note} "
            "If the answer is not in the context, say 'I cannot find this in your documents.' "
            "Cite the context blocks inline like [chunk 3] or [doc <id> | chunk 3]."
        )
        user_prompt = f"Context:\n{context}\n\nQuestion: {question}"
        result = await self.llm.generate(
            messages=[
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=user_prompt),
            ],
            temperature=0.2,
            max_tokens=512,
        )
        return {
            "question": question,
            "answer": result.answer,
            "model": result.model,
            "citations": [
                {
                    "chunk_id": h.chunk_id,
                    "chunk_index": h.chunk_index,
                    **({"document_id": h.document_id} if hasattr(h, "document_id") else {}),
                    "score": h.score,
                }
                for h in hits
            ],
        }


# Helpers

# No need if pgvector is live
def _rank(chunks, query_vector):
    scored: list[tuple[float, object]] = []
    for c in chunks:
        if not c.embeddings:
            continue
        vec = c.embedding.get("vector")
        if not vec:
            continue
        scored.append((_cosine(query_vector, vec), c))
    scored.sort(key=lambda s:s[0], reverse=True)
    return scored


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0