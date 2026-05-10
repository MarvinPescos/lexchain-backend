"""
    This is for DEV only. No-op first
"""


from app.infrastructure.observability.logging_setup import log
from app.infrastructure.nlp.ocr import build_ocr
from app.infrastructure.nlp.analysis import LLMAnalysisProvider
from app.infrastructure.nlp.chunking import SpacySentenceChunker
from app.infrastructure.nlp.embeddings import build_embedder
from app.workers import process_document_job

import uuid
import asyncio


class LoggingDocumentQueue:
    """
    Temporary queue implementation.

    Replace this with Celery/RQ/SQS/etc. later while keeping the same interface.
    """

    async def enqueue_process_document(
        self,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
        storage_url: str,
        content_type: str,
        document_hash: str,
    ) -> None:

        asyncio.create_task(
            process_document_job(
                document_id=document_id,
                ocr=build_ocr(storage_url),
                analyzer=LLMAnalysisProvider(),
                chunker=SpacySentenceChunker(800, 100),
                embedder=build_embedder(),
            )
        )

        log.info(
            "document.queue.enqueued",
            document_id=str(document_id),
            user_id=str(user_id),
            storage_url=storage_url,
            content_type=content_type,
            document_hash=document_hash,
        )
