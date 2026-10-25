import asyncio
import uuid

from app.features.chunks.repository import DocumentChunkRepository
from app.features.documents.repository import DocumentRepository
from app.infrastructure.nlp.embeddings import Embedder
from app.infrastructure.nlp.llm import LLMMessage, LLMProvider
from app.infrastructure.observability.logging_setup import log
from app.shared.errors.exceptions import NotFoundError
from langfuse import get_client, observe, propagate_attributes
from sqlalchemy.ext.asyncio import AsyncSession

from .schemas import (
    GlobalSearchHit,
    GlobalSearchResponse,
    RegisterReference,
    SearchHit,
    SearchResponse,
)


class RetrievalService:
    def __init__(self, session: AsyncSession, embedder: Embedder) -> None:
        self.session = session
        self.embedder = embedder
        self.doc_repo = DocumentRepository(session)
        self.chunk_repo = DocumentChunkRepository(session)

    async def search_document(
        self, user_id: uuid.UUID, document_id: uuid.UUID, query: str, top_k: int = 5
    ) -> SearchResponse:

        doc = await self.doc_repo.get_one(user_id, document_id)

        if not doc:
            is_party = await self.doc_repo.is_party(user_id, document_id)
            if not is_party:
                raise NotFoundError("Document not found")
            doc = await self.doc_repo.get_by_id(document_id)

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
            ],
        )

    # Semantic search
    async def search_all(
        self,
        user_id: uuid.UUID,
        query: str,  # basta something naa sa docs
        top_k: int = 3,
    ) -> GlobalSearchResponse:

        q_embed = await self.embedder.embed(query)
        top = await self.chunk_repo.search_user(user_id, q_embed.vector, top_k)

        log.info(
            "retrieval.search.global",
            user_id=str(user_id),
            query_len=len(query),
            returned=len(top),
        )

        return GlobalSearchResponse(
            query=query,
            results=[
                GlobalSearchHit(
                    chunk_id=c.id,
                    document_id=c.document_id,
                    chunk_index=c.chunk_index,
                    score=float(score),
                    file_name=doc.file_name,
                    created_at=doc.created_at,
                    # A document is filed with book, doc and page numbers or none (DB check).
                    register_reference=RegisterReference(
                        doc_no=doc.doc_no,
                        page_no=doc.page_no,
                        book_number=book.book_number,
                        series_year=book.series_year,
                    )
                    if book and doc.doc_no is not None and doc.page_no is not None
                    else None,
                )
                for c, doc, book, score in top
            ],
        )


class RAGService:
    def __init__(self, retrieval: RetrievalService, llm: LLMProvider) -> None:
        self.retrieval = retrieval
        self.llm = llm

    @observe(name="RAG_Question_Answer")
    async def ask_document(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        question: str,
        top_k: int = 2,
    ):
        langfuse = get_client()

        with propagate_attributes(
            user_id=str(user_id),
            metadata={"document_id": str(document_id)},
        ):
            # Set the question as the input on the current span
            langfuse.update_current_span(input=question)

            hits = await self.retrieval.search_document(
                user_id, document_id, question, top_k
            )
            context = "\n\n".join(
                f"[chunk {h.chunk_index}]\n{h.text}" for h in hits.results
            )

            result = await self._answer(
                question, context, hits.results, scoped_to_document=True
            )

        await asyncio.to_thread(langfuse.flush)
        return result

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
            else "You are a legal document assistant answering across the user's entire document library. Answer ONLY using the provided context."
        )
        system_prompt = (
            f"{scope_note}\n\n"
            "Be a helpful, intelligent conversational assistant. "
            "If the user asks a factual question, rely strictly on the Context. If the answer is completely missing, say 'I cannot find this in your documents.' "
            "If the user is just chatting, asking for overviews, or making general requests, engage politely and use the Context to help them."
        )

        messages = [LLMMessage(role="system", content=system_prompt)]

        user_prompt = f"Context:\n{context}\n\nQuestion: {question}"
        messages.append(LLMMessage(role="user", content=user_prompt))

        result = await self.llm.generate(
            messages=messages,
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
                    **(
                        {"document_id": h.document_id}
                        if hasattr(h, "document_id")
                        else {}
                    ),
                    "score": h.score,
                }
                for h in hits
            ],
        }
