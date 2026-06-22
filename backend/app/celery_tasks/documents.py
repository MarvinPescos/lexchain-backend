import asyncio
import uuid

import app.all_models  # noqa: F401 — registers all SQLAlchemy models for the worker process
from app.celery_app import celery_app
from app.workers.process_documents import process_document_job


@celery_app.task(
    name="document.process",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def process_document_task(self, document_id: uuid.UUID, storage_url: str):
    from app.infrastructure.nlp.analysis import LLMAnalysisProvider
    from app.infrastructure.nlp.chunking import SpacySentenceChunker
    from app.infrastructure.nlp.embeddings import build_embedder

    try:
        asyncio.run(
            process_document_job(
                document_id=document_id,
                storage_url=storage_url,
                analyzer=LLMAnalysisProvider(),
                chunker=SpacySentenceChunker(800, 100),
                embedder=build_embedder(),
            )
        )
    except Exception as exc:
        raise self.retry(exc=exc)
