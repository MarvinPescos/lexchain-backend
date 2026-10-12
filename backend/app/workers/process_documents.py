import uuid

import httpx
from langfuse import get_client, propagate_attributes
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_tasks import handle_document_processed
from app.features.chunks.repository import DocumentChunkRepository
from app.features.documents.repository import DocumentRepository
from app.features.documents.extraction.repository import DocumentExtractionRepository
from app.features.insights.repository import DocumentInsightRepository
from app.infrastructure.database import AsyncSessionLocal
from app.infrastructure.nlp.analysis.base import AnalysisProvider
from app.infrastructure.nlp.chunking.base import Chunker
from app.infrastructure.nlp.embeddings.base import Embedder
from app.infrastructure.nlp.ocr import build_ocr
from app.infrastructure.observability.logging_setup import log

from app.features.documents.constants import (
    STATUS_AWAITING_REVIEW,
    STATUS_COMPLETED,
    STATUS_ENRICHING,
    STATUS_FAILED,
    STATUS_PROCESSING,
)
from app.features.documents.extraction.blocks import blocks_from_text
from app.features.documents.extraction.flagging import build_flags


langfuse = get_client()


async def extract_document_job(
    document_id: uuid.UUID,
    storage_url: str,
) -> None:
    """Phase 1: OCR the file and hand it to the issuer for review.

    Stops at AWAITING_REVIEW. Chunking, embedding and analysis deliberately do
    not run here — building them from unreviewed text would mean the RAG index
    and the on-chain snapshot disagree the moment the issuer corrects anything.
    """
    async with AsyncSessionLocal() as session:
        doc_repo = DocumentRepository(session)
        extraction_repo = DocumentExtractionRepository(session)

        doc = await doc_repo.get_by_id(document_id)
        if not doc:
            log.warning("worker.document.missing", document_id=str(document_id))
            return
        stable_document_id = document_id
        stable_document_id_str = str(stable_document_id)
        doc_user_id = doc.user_id
        doc_file_name = doc.file_name

        with langfuse.start_as_current_observation(
            as_type="span",
            name="document_extraction",
            input={"document_id": str(document_id)},
        ) as root_span:
            with propagate_attributes(
                user_id=str(doc_user_id),
                metadata={"filename": doc_file_name},
            ):
                try:
                    # Download the file from Supabase
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(storage_url)
                        resp.raise_for_status()
                        file_data = resp.content

                    await _mark_processing(session, doc_repo, doc)

                    # Build OCR provider from downloaded bytes
                    ocr = build_ocr(file_data)

                    ocr_result, extraction = await _run_ocr_and_store_extraction(
                        session, ocr, extraction_repo, doc, file_data
                    )

                    flag_count = len(extraction.flags or [])
                    await doc_repo.update(doc, status=STATUS_AWAITING_REVIEW)
                    await session.commit()

                    handle_document_processed.delay(
                        user_id=str(doc_user_id),
                        document_title=doc_file_name,
                        document_id=str(stable_document_id),
                        status="awaiting_review",
                    )

                    log.info(
                        "worker.document.awaiting_review",
                        document_id=stable_document_id_str,
                        pages=ocr_result.page_count,
                        blocks=len(extraction.blocks or []),
                        flags=flag_count,
                    )

                    root_span.update(
                        output={
                            "status": "awaiting_review",
                            "blocks": len(extraction.blocks or []),
                            "flags": flag_count,
                        }
                    )

                except Exception as e:
                    root_span.update(output={"status": "failed", "error": str(e)})
                    await session.rollback()
                    log.error(
                        "worker.document.extract.failed",
                        document_id=stable_document_id_str,
                        error=str(e),
                    )
                    await _mark_failed(stable_document_id)

                    handle_document_processed.delay(
                        user_id=str(doc_user_id),
                        document_title=doc_file_name,
                        document_id=str(stable_document_id),
                        status="failed",
                    )

        # Flush outside the with block — span is fully closed before sending
        langfuse.flush()


async def enrich_document_job(
    document_id: uuid.UUID,
    analyzer: AnalysisProvider,
    chunker: Chunker,
    embedder: Embedder,
) -> None:
    """Phase 2: chunk, embed and analyze the text the issuer approved.

    Runs only after approval, so every derived artefact is built from the same
    text that finalize will snapshot and anchor.
    """
    async with AsyncSessionLocal() as session:
        doc_repo = DocumentRepository(session)
        extraction_repo = DocumentExtractionRepository(session)
        insight_repo = DocumentInsightRepository(session)
        chunk_repo = DocumentChunkRepository(session)

        doc = await doc_repo.get_by_id(document_id)
        if not doc:
            log.warning("worker.document.missing", document_id=str(document_id))
            return

        extraction = await extraction_repo.get_by_document_id(document_id)
        if not extraction:
            log.error("worker.document.extraction.missing", document_id=str(document_id))
            await _mark_failed(document_id)
            return

        stable_document_id_str = str(document_id)
        doc_user_id = doc.user_id
        doc_file_name = doc.file_name
        approved_text = extraction.approved_text

        with langfuse.start_as_current_observation(
            as_type="span",
            name="document_enrichment",
            input={"document_id": str(document_id)},
        ) as root_span:
            with propagate_attributes(
                user_id=str(doc_user_id),
                metadata={"filename": doc_file_name},
            ):
                try:
                    await doc_repo.update(doc, status=STATUS_ENRICHING)
                    await session.commit()

                    # 1. Chunk
                    chunk_result, stored_chunks = await _run_chunking_and_store_chunks(
                        session, chunker, chunk_repo, doc, extraction, approved_text
                    )
                    # 2. Embed
                    embeddings_dim = await _run_embeddings(
                        session,
                        embedder,
                        chunk_repo,
                        chunks_created=chunk_result.chunks,
                        stored_chunks=stored_chunks,
                    )
                    # 3. Analyze
                    analysis_result = await _run_analysis_and_store_insight(
                        analyzer, insight_repo, doc, extraction, approved_text
                    )

                    await _mark_completed(session, doc_repo, doc)

                    handle_document_processed.delay(
                        user_id=str(doc_user_id),
                        document_title=doc_file_name,
                        document_id=stable_document_id_str,
                        status="completed",
                    )

                    log.info(
                        "worker.document.completed",
                        document_id=stable_document_id_str,
                        chunks=len(chunk_result.chunks),
                        labels=analysis_result.labels,
                        embeddings_dim=embeddings_dim,
                        edited_blocks=extraction.edited_block_count,
                    )

                    root_span.update(
                        output={
                            "status": "completed",
                            "chunks": len(chunk_result.chunks),
                        }
                    )

                except Exception as e:
                    root_span.update(output={"status": "failed", "error": str(e)})
                    await session.rollback()
                    log.error(
                        "worker.document.enrich.failed",
                        document_id=stable_document_id_str,
                        error=str(e),
                    )
                    await _mark_failed(document_id)

                    handle_document_processed.delay(
                        user_id=str(doc_user_id),
                        document_title=doc_file_name,
                        document_id=stable_document_id_str,
                        status="failed",
                    )

        langfuse.flush()


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
    with langfuse.start_as_current_observation(
        as_type="span",
        name="ocr_extraction",
        input={"file_size_bytes": len(file_data)},
    ) as span:
        ocr_result = await ocr.extract(file_data)
        span.update(
            output={
                "page_count": ocr_result.page_count,
                "text_length": len(ocr_result.text),
                "engine": (ocr_result.meta or {}).get("engine"),
                "text_preview": ocr_result.text[:300],
            }
        )
    # span auto-closes on exit

    # Engines that report layout give us positioned blocks; the rest get
    # paragraph blocks so the review flow is the same either way.
    blocks = ocr_result.blocks or blocks_from_text(ocr_result.ingestion_text)
    flags = build_flags(blocks, ocr_result.ingestion_text)

    extraction = await extraction_repo.create(
        document_id=doc.id,
        engine=(ocr_result.meta or {}).get("engine", "unknown"),
        engine_version=(ocr_result.meta or {}).get("engine_version"),
        text=ocr_result.text,
        page_count=ocr_result.page_count,
        confidence_avg=(ocr_result.meta or {}).get("confidence_avg"),
        meta=ocr_result.meta or {},
        blocks=blocks,
        flags=flags,
    )

    # OCR runs after finalize, on the frozen signed copy. Nothing here touches
    # the anchored hash — that is the SHA-256 of the PDF, set at finalize.

    await session.commit()
    return ocr_result, extraction


async def _run_analysis_and_store_insight(
    analyzer: AnalysisProvider,
    insight_repo: DocumentInsightRepository,
    doc,
    extraction,
    text: str,
):
    with langfuse.start_as_current_observation(
        as_type="generation",
        name="analysis_llm",
        model="meta/llama-3.1-70b-instruct",
        input=text[:24_000],
    ) as generation:
        analysis_result = await analyzer.analyze(text)
        generation.update(
            output={
                "summary": analysis_result.summary,
                "labels": analysis_result.labels,
                "entity_count": len(analysis_result.entities or []),
                "risk_flag_count": len(analysis_result.risk_flags or []),
                "confidence": analysis_result.confidence,
            }
        )
    # generation auto-closes on exit

    await insight_repo.create(
        document_id=doc.id,
        extraction_id=extraction.id,
        engine=(analysis_result.meta or {}).get("engine", "unknown"),
        engine_version=(analysis_result.meta or {}).get("engine_version"),
        summary=analysis_result.summary,
        labels={"items": analysis_result.labels} if analysis_result.labels else None,
        entities={"items": analysis_result.entities}
        if analysis_result.entities
        else None,
        risk_flags={"items": analysis_result.risk_flags}
        if analysis_result.risk_flags
        else None,
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
    with langfuse.start_as_current_observation(
        as_type="span",
        name="chunking",
        input={"text_length": len(text)},
    ) as span:
        chunk_result = await chunker.split(text)
        span.update(
            output={
                "chunk_count": len(chunk_result.chunks),
                "strategy": chunk_result.strategy,
                "first_chunk_preview": chunk_result.chunks[0].text[:200]
                if chunk_result.chunks
                else None,
            }
        )
    # span auto-closes on exit

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
    chunks_created,  # list[Chunk] with text
    stored_chunks,  # list[DocumentChunk] with id
) -> None:
    with langfuse.start_as_current_observation(
        as_type="span",
        name="embedding",
        input={"chunk_count": len(chunks_created)},
    ) as span:
        if not stored_chunks:
            span.update(output={"skipped": True})
            return None

        texts = [c.text for c in chunks_created]
        results = await embedder.embed_batch(texts)

        for chunk, result in zip(stored_chunks, results):
            await chunk_repo.set_embedding(
                chunk.id,
                vector=result.vector,
                meta=result.meta,
            )
        await session.commit()

        span.update(
            output={
                "dim": results[0].dim if results else None,
                "model": "all-MiniLm-L6-v2",
            }
        )
    # span auto-closes on exit

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
        log.error(
            "worker.document.failed.status_update_failed", document_id=str(document_id)
        )
