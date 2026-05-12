import uuid
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.observability.logging_setup import log
from app.infrastructure.database import AsyncSessionLocal
from app.infrastructure.nlp.ocr import build_ocr
from app.infrastructure.nlp.analysis.base import AnalysisProvider
from app.infrastructure.nlp.chunking.base import Chunker
from app.infrastructure.nlp.embeddings.base import Embedder

from app.features.documents.repository import DocumentRepository
from app.features.extraction.repository import DocumentExtractionRepository
from app.features.insights.repository import DocumentInsightRepository
from app.features.chunks.repository import DocumentChunkRepository

STATUS_PROCESSING = "PROCESSING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"

async def process_document_job(
    document_id: uuid.UUID,
    storage_url: str,
    analyzer: AnalysisProvider,
    chunker: Chunker,
    embedder: Embedder,
) -> None:
    async with AsyncSessionLocal() as session:
        doc_repo = DocumentRepository(session)
        extraction_repo = DocumentExtractionRepository(session)
        insight_repo = DocumentInsightRepository(session)
        chunk_repo = DocumentChunkRepository(session)

        doc = await doc_repo.get_by_id(document_id)
        if not doc:
            log.warning("worker.document.missing", document_id=str(document_id))
            return
        stable_document_id = document_id
        stable_document_id_str = str(stable_document_id)

        try:
            # Download the file from Supabase
            async with httpx.AsyncClient() as client:
                resp = await client.get(storage_url)
                resp.raise_for_status()
                file_data = resp.content

            await _mark_processing(session, doc_repo, doc)

            # Build OCR provider from downloaded bytes
            ocr = build_ocr(file_data)

        #1
            ocr_result, extraction = await _run_ocr_and_store_extraction(
                session,
                ocr,
                extraction_repo,
                doc,
                file_data,
            )
        #2
            analysis_result = await _run_analysis_and_store_insight(
                analyzer,
                insight_repo,
                doc,
                extraction,
                ocr_result.text,
            )
        #3
            chunk_result, stored_chunks = await _run_chunking_and_store_chunks(
                session,
                chunker,
                chunk_repo,
                doc,
                extraction,
                ocr_result.text,
            )
        # 4. Embeddings
            embeddings_dim = await _run_embeddings(
                session,
                embedder,
                chunk_repo,
                chunks_created=chunk_result.chunks,
                stored_chunks=stored_chunks,  # returned from _run_chunking_and_store_chunks
            )

            await _mark_completed(session, doc_repo, doc)
            log.info(
                "worker.document.completed",
                document_id=stable_document_id_str,
                pages=ocr_result.page_count,
                chunks=len(chunk_result.chunks),
                labels=analysis_result.labels,
                embeddings_dim=embeddings_dim,
            )

        except Exception as e:
            await session.rollback()
            log.error(
                "worker.document.failed",
                document_id=stable_document_id_str,
                error=str(e),
            )
            await _mark_failed(stable_document_id)

# Helpers

async def _mark_processing(
    session: AsyncSession,
    doc_repo: DocumentRepository,
    doc,
) -> None:
    await doc_repo.update(doc, status=STATUS_PROCESSING)
    await session.commit()


async def _run_ocr_and_store_extraction(
    session: AsyncSession,
    ocr,
    extraction_repo: DocumentExtractionRepository,
    doc,
    file_data: bytes,
):
    ocr_result = await ocr.extract(file_data)
    extraction = await extraction_repo.create(
        document_id=doc.id,
        engine=(ocr_result.meta or {}).get("engine", "unknown"),
        engine_version=(ocr_result.meta or {}).get("engine_version"),
        text=ocr_result.text,
        page_count=ocr_result.page_count,
        confidence_avg=(ocr_result.meta or {}).get("confidence_avg"),
        meta=ocr_result.meta or {},
    )
    await session.commit()
    return ocr_result, extraction


async def _run_analysis_and_store_insight(
    analyzer: AnalysisProvider,
    insight_repo: DocumentInsightRepository,
    doc,
    extraction,
    text: str,
):
    analysis_result = await analyzer.analyze(text)
    await insight_repo.create(
        document_id=doc.id,
        extraction_id=extraction.id,
        engine=(analysis_result.meta or {}).get("engine", "unknown"),
        engine_version=(analysis_result.meta or {}).get("engine_version"),
        summary=analysis_result.summary,
        labels={"items": analysis_result.labels} if analysis_result.labels else None,
        entities={"items": analysis_result.entities} if analysis_result.entities else None,
        risk_flags={"items": analysis_result.risk_flags} if analysis_result.risk_flags else None,
        confidence=analysis_result.confidence,
        meta=analysis_result.meta,
    )
    return analysis_result

async def _run_chunking_and_store_chunks(
    session: AsyncSession,
    chunker: Chunker,
    chunk_repo: DocumentChunkRepository,
    doc,
    extraction,
    text: str,
):
    chunk_result = await chunker.split(text)

    # NOTE: fix typo in ChunkResult: use .strategy everywhere (your base has `stategy`)
    strategy = getattr(chunk_result, "strategy", None) or getattr(
        chunk_result, "stategy", "unknown"
    )

    engine = (chunk_result.meta or {}).get("engine", "unknown")
    engine_version = (chunk_result.meta or {}).get("engine_version")

    rows = [
        {
            "document_id": doc.id,
            "extraction_id": extraction.id,
            "chunk_index": ch.index,
            "text": ch.text,
            "char_count": ch.char_count,
            "embedding": None,  # fill in embedding stage later
            "strategy": strategy,
            "engine": engine,
            "engine_version": engine_version,
            "meta": ch.meta,
        }
        for ch in chunk_result.chunks
    ]

    stored = []

    if rows:
        stored = await chunk_repo.bulk_create(rows)
        await session.commit()

    return chunk_result, stored


async def _run_embeddings(
    session,
    embedder: Embedder,
    chunk_repo: DocumentChunkRepository,
    chunks_created,          # list[Chunk] with text
    stored_chunks,           # list[DocumentChunk] with id
) -> None:
    if not stored_chunks:
        return

    texts = [c.text for c in chunks_created]
    results = await embedder.embed_batch(texts)

    for chunk, result in zip(stored_chunks, results):
        await chunk_repo.set_embedding(
            chunk.id,
            vector=result.vector,
            meta=result.meta,
        )
    await session.commit()
    return results[0].dim if results else None


async def _mark_completed(
    session: AsyncSession,
    doc_repo: DocumentRepository,
    doc,
) -> None:
    await doc_repo.update(doc, status=STATUS_COMPLETED)
    await session.commit()


async def _mark_failed(document_id: uuid.UUID) -> None:
    try:
        async with AsyncSessionLocal() as fail_session:
            fail_repo = DocumentRepository(fail_session)
            fresh = await fail_repo.get_by_id(document_id)
            if fresh:
                await fail_repo.update(fresh, status=STATUS_FAILED)
                await fail_session.commit()
    except Exception:
        log.error("worker.document.failed.status_update_failed", document_id=str(document_id))