"""Rebuild chunk vectors that are missing, e.g. after the embedding model changed.

Chunk text is kept, so only the embedding step reruns — no OCR, review or
analysis. Each document commits on its own so a failure leaves the rest done.
"""

import uuid

from sqlalchemy import select

from app.features.chunks.models import DocumentChunk
from app.features.chunks.repository import DocumentChunkRepository
from app.infrastructure.database import AsyncSessionLocal
from app.infrastructure.nlp.embeddings import Embedder, build_embedder
from app.infrastructure.observability.logging_setup import log


async def documents_missing_vectors() -> list[uuid.UUID]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DocumentChunk.document_id)
            .where(DocumentChunk.embedding_vec.is_(None))
            .distinct()
        )
        return list(result.scalars().all())


async def reembed_document(document_id: uuid.UUID, embedder: Embedder) -> int:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DocumentChunk)
            .where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.embedding_vec.is_(None),
            )
            .order_by(DocumentChunk.chunk_index)
        )
        chunks = list(result.scalars().all())
        if not chunks:
            return 0

        repo = DocumentChunkRepository(session)
        results = await embedder.embed_batch([c.text for c in chunks])
        for chunk, embedded in zip(chunks, results):
            await repo.set_embedding(chunk.id, vector=embedded.vector, meta=embedded.meta)
        await session.commit()
        return len(chunks)


async def reembed_missing_chunks(embedder: Embedder | None = None) -> int:
    embedder = embedder or build_embedder()
    total = 0
    for document_id in await documents_missing_vectors():
        try:
            total += await reembed_document(document_id, embedder)
        except Exception as e:
            log.error(
                "chunks.reembed.failed", document_id=str(document_id), error=str(e)
            )
    log.info("chunks.reembed.done", chunks=total)
    return total
