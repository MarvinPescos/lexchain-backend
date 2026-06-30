"""
This is for DEV only. No-op first
"""

import asyncio
import uuid

from app.infrastructure.nlp.analysis import LLMAnalysisProvider
from app.infrastructure.nlp.chunking import build_chunker
from app.infrastructure.nlp.embeddings import build_embedder
from app.infrastructure.observability.logging_setup import log


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
        from app.workers.process_documents import process_document_job

        asyncio.create_task(
            process_document_job(
                document_id=document_id,
                storage_url=storage_url,
                analyzer=LLMAnalysisProvider(),
                chunker=build_chunker(),
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
