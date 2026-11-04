import uuid
from collections.abc import Awaitable, Callable
from functools import partial

import httpx
from langfuse import get_client, propagate_attributes
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_tasks import handle_document_processed
from app.features.chunks.repository import DocumentChunkRepository
from app.features.documents.repository import DocumentRepository
from app.features.documents.extraction.repository import DocumentExtractionRepository
from app.features.insights.repository import DocumentInsightRepository
from app.infrastructure.clients.supabase_storage import get_storage_client
from app.infrastructure.database import AsyncSessionLocal
from app.infrastructure.nlp.analysis.base import (
    ANALYSIS_STATUS_COMPLETE,
    AnalysisProvider,
)
from app.infrastructure.nlp.chunking.base import Chunker
from app.infrastructure.nlp.embeddings.base import Embedder
from app.infrastructure.nlp.ocr import build_ocr
from app.infrastructure.observability.logging_setup import log
from app.shared.errors.exceptions import BadRequestError

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
    signed_copy_id: uuid.UUID | str | None = None,
    final_attempt: bool = True,
) -> None:
    """Phase 1: OCR the signed copy and hand it to the issuer for review.

    Stops at AWAITING_REVIEW. Chunking, embedding and analysis deliberately do
    not run here — they are built from the text the issuer approves.

    The job is for one signed copy. If the lawyer replaced it, the job does
    nothing, and no commit of a stale job lands after the replace (see
    `_commit_if_current`). None means the current copy, for messages queued
    before jobs named their copy.

    A transient error is raised for Celery to retry unless this is the
    `final_attempt`; only then is the document marked FAILED. A job delivered
    again after OCR finished does nothing.
    """
    retry_error: Exception | None = None
    async with AsyncSessionLocal() as session:
        doc_repo = DocumentRepository(session)
        extraction_repo = DocumentExtractionRepository(session)

        doc = await doc_repo.get_by_id(document_id)
        if not doc:
            log.warning("worker.document.missing", document_id=str(document_id))
            return
        copy_id = _as_uuid(signed_copy_id) or doc.signed_copy_id
        if copy_id != doc.signed_copy_id:
            _log_replaced("extract", document_id, copy_id)
            return
        if doc.status in OCR_DONE_STATUSES:
            log.info(
                "worker.document.extract.skipped",
                document_id=str(document_id),
                status=doc.status,
            )
            return
        stable_document_id = document_id
        stable_document_id_str = str(stable_document_id)
        doc_user_id = doc.user_id
        doc_file_name = doc.file_name
        commit = partial(_commit_if_current, session, document_id, copy_id)

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
                    file_data = await get_storage_client().download(storage_url)

                    await _mark_processing(commit, doc_repo, doc)

                    # Build OCR provider from downloaded bytes
                    ocr = build_ocr(file_data)

                    ocr_result, extraction = await _run_ocr_and_store_extraction(
                        session, ocr, extraction_repo, doc, file_data
                    )

                    # The extraction and the status commit together, so a
                    # retry never finds an extraction it did not finish.
                    flag_count = len(extraction.flags or [])
                    await doc_repo.update(doc, status=STATUS_AWAITING_REVIEW)
                    await commit()

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

                except SignedCopyReplaced:
                    await session.rollback()
                    root_span.update(output={"status": "replaced"})
                    _log_replaced("extract", document_id, copy_id)
                except Exception as e:
                    await session.rollback()
                    if not final_attempt and is_retryable(e):
                        root_span.update(output={"status": "retrying", "error": str(e)})
                        log.warning(
                            "worker.document.extract.retrying",
                            document_id=stable_document_id_str,
                            error=str(e),
                        )
                        retry_error = e
                    else:
                        root_span.update(output={"status": "failed", "error": str(e)})
                        log.error(
                            "worker.document.extract.failed",
                            document_id=stable_document_id_str,
                            error=str(e),
                        )
                        if await _mark_failed(stable_document_id, copy_id):
                            handle_document_processed.delay(
                                user_id=str(doc_user_id),
                                document_title=doc_file_name,
                                document_id=str(stable_document_id),
                                status="failed",
                            )

        # Flush outside the with block — span is fully closed before sending
        langfuse.flush()

    # Raised only now, so the trace is sent; the Celery task retries on it.
    if retry_error is not None:
        raise retry_error


async def enrich_document_job(
    document_id: uuid.UUID,
    analyzer: AnalysisProvider,
    chunker: Chunker,
    embedder: Embedder,
    signed_copy_id: uuid.UUID | str | None = None,
    final_attempt: bool = True,
) -> None:
    """Phase 2: chunk, embed and analyze the text the issuer approved.

    Runs only after approval, while the document is still SIGNED, so the
    lawyer sees the risk clauses before finalizing. Nothing built here feeds
    the anchored hash, which is the SHA-256 of the signed PDF.

    Runs only while the document is ENRICHING, and like extract_document_job
    does nothing for a signed copy that has been replaced. Retries like
    extract_document_job. Each run first clears the chunks and
    insights an earlier, interrupted run left behind, so a retry rebuilds
    them rather than duplicating them. A job delivered again after the
    document completed does nothing.
    """
    retry_error: Exception | None = None
    async with AsyncSessionLocal() as session:
        doc_repo = DocumentRepository(session)
        extraction_repo = DocumentExtractionRepository(session)
        insight_repo = DocumentInsightRepository(session)
        chunk_repo = DocumentChunkRepository(session)

        doc = await doc_repo.get_by_id(document_id)
        if not doc:
            log.warning("worker.document.missing", document_id=str(document_id))
            return

        copy_id = _as_uuid(signed_copy_id) or doc.signed_copy_id
        if copy_id != doc.signed_copy_id:
            _log_replaced("enrich", document_id, copy_id)
            return

        # Approve and retry set ENRICHING; anything else means the job is late
        # (already completed) or its text was cleared by a replace.
        if doc.status != STATUS_ENRICHING:
            log.info(
                "worker.document.enrich.skipped",
                document_id=str(document_id),
                status=doc.status,
            )
            return

        extraction = await extraction_repo.get_by_document_id(document_id)
        if not extraction:
            log.error("worker.document.extraction.missing", document_id=str(document_id))
            await _mark_failed(document_id, copy_id)
            return
        commit = partial(_commit_if_current, session, document_id, copy_id)

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
                    # Whatever an interrupted run stored is rebuilt from scratch.
                    await chunk_repo.delete_for_document(doc.id)
                    await insight_repo.delete_for_document(doc.id)
                    await commit()

                    # 1. Chunk
                    chunk_result, stored_chunks = await _run_chunking_and_store_chunks(
                        commit, chunker, chunk_repo, doc, extraction, approved_text
                    )
                    # 2. Embed
                    embeddings_dim = await _run_embeddings(
                        commit,
                        embedder,
                        chunk_repo,
                        chunks_created=chunk_result.chunks,
                        stored_chunks=stored_chunks,
                    )
                    # 3. Analyze
                    analysis_result = await _run_analysis_and_store_insight(
                        analyzer, insight_repo, doc, extraction, approved_text
                    )

                    await _mark_completed(commit, doc_repo, doc)

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
                        risk_flags=len(analysis_result.risk_flags or []),
                        analysis_status=(analysis_result.meta or {}).get(
                            "analysis_status", ANALYSIS_STATUS_COMPLETE
                        ),
                        embeddings_dim=embeddings_dim,
                        edited_blocks=extraction.edited_block_count,
                    )

                    root_span.update(
                        output={
                            "status": "completed",
                            "chunks": len(chunk_result.chunks),
                        }
                    )

                except SignedCopyReplaced:
                    await session.rollback()
                    root_span.update(output={"status": "replaced"})
                    _log_replaced("enrich", document_id, copy_id)
                except Exception as e:
                    await session.rollback()
                    if not final_attempt and is_retryable(e):
                        root_span.update(output={"status": "retrying", "error": str(e)})
                        log.warning(
                            "worker.document.enrich.retrying",
                            document_id=stable_document_id_str,
                            error=str(e),
                        )
                        retry_error = e
                    else:
                        root_span.update(output={"status": "failed", "error": str(e)})
                        log.error(
                            "worker.document.enrich.failed",
                            document_id=stable_document_id_str,
                            error=str(e),
                        )
                        if await _mark_failed(document_id, copy_id):
                            handle_document_processed.delay(
                                user_id=str(doc_user_id),
                                document_title=doc_file_name,
                                document_id=stable_document_id_str,
                                status="failed",
                            )

        langfuse.flush()

    if retry_error is not None:
        raise retry_error


# Helpers

# Commits the job's writes, only while its signed copy is still current.
Commit = Callable[[], Awaitable[None]]

# A job for a document already past OCR was delivered twice: nothing to do.
OCR_DONE_STATUSES = (STATUS_AWAITING_REVIEW, STATUS_ENRICHING, STATUS_COMPLETED)


class SignedCopyReplaced(Exception):
    """The signed copy a job was working on is no longer the current one."""


def _as_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    # Celery delivers ids as strings.
    if value is None or isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(value)


def _log_replaced(job: str, document_id: uuid.UUID, copy_id: uuid.UUID | None) -> None:
    log.info(
        f"worker.document.{job}.copy_replaced",
        document_id=str(document_id),
        signed_copy_id=str(copy_id),
    )


async def _commit_if_current(
    session: AsyncSession, document_id: uuid.UUID, signed_copy_id: uuid.UUID | None
) -> None:
    """Commit only while `signed_copy_id` is still the document's current copy.

    Locks the document row first. Replacing the signed copy takes the same
    lock, so either this commit lands before the replace (which then clears
    it), or the replace lands first and this raises SignedCopyReplaced.
    """
    current = await DocumentRepository(session).lock_signed_copy_id(document_id)
    if current != signed_copy_id:
        raise SignedCopyReplaced(str(signed_copy_id))
    await session.commit()


def is_retryable(error: Exception) -> bool:
    """Whether trying again could succeed.

    Only errors known to be permanent — a provider rejecting the request, or
    our own validation — skip the retries. Anything else (timeouts, refused
    connections, 5xx, rate limits, database hiccups) is worth another try.
    """
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return status >= 500 or status in (408, 429)
    return not isinstance(error, BadRequestError)



async def _mark_processing(commit: Commit, doc_repo: DocumentRepository, doc) -> None:
    await doc_repo.update(doc, status=STATUS_PROCESSING)
    await commit()


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

    # An earlier attempt may have stored an extraction before failing.
    previous = await extraction_repo.get_by_document_id(doc.id)
    if previous is not None:
        await extraction_repo.delete(previous)

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

    # OCR runs on the signed copy before finalize. Nothing here touches the
    # anchored hash — that is the SHA-256 of the PDF, taken at finalize.

    # Not committed here: the caller commits it with AWAITING_REVIEW.
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
        input={"text_length": len(text), "text_preview": text[:2_000]},
    ) as generation:
        analysis_result = await analyzer.analyze(text)
        generation.update(
            output={
                "summary": analysis_result.summary,
                "labels": analysis_result.labels,
                "entity_count": len(analysis_result.entities or []),
                "risk_flag_count": len(analysis_result.risk_flags or []),
                "confidence": analysis_result.confidence,
                "analysis_status": (analysis_result.meta or {}).get(
                    "analysis_status"
                ),
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
    commit: Commit,
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
        await commit()

    return chunk_result, stored


async def _run_embeddings(
    commit: Commit,
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
        await commit()

        span.update(
            output={
                "dim": results[0].dim if results else None,
                "model": (results[0].meta or {}).get("engine_version")
                if results
                else None,
            }
        )
    # span auto-closes on exit

    return results[0].dim if results else None


async def _mark_completed(commit: Commit, doc_repo: DocumentRepository, doc) -> None:
    await doc_repo.update(doc, status=STATUS_COMPLETED)
    await commit()


async def _mark_failed(document_id: uuid.UUID, signed_copy_id: uuid.UUID | None) -> bool:
    """Mark the document FAILED, unless its signed copy was replaced meanwhile.

    True when it was marked, so the caller notifies only then.
    """
    try:
        async with AsyncSessionLocal() as fail_session:
            fail_repo = DocumentRepository(fail_session)
            fresh = await fail_repo.get_by_id(document_id)
            if not fresh:
                return False
            await fail_repo.update(fresh, status=STATUS_FAILED)
            await _commit_if_current(fail_session, document_id, signed_copy_id)
            return True
    except SignedCopyReplaced:
        _log_replaced("mark_failed", document_id, signed_copy_id)
        return False
    except Exception:
        log.error(
            "worker.document.failed.status_update_failed", document_id=str(document_id)
        )
        return False
