import uuid

import app.all_models  # noqa: F401 — registers all SQLAlchemy models for the worker process
from app.celery_app import celery_app
from app.celery_tasks.runner import run_async
from app.workers.process_documents import enrich_document_job, extract_document_job


@celery_app.task(
    name="document.extract",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def extract_document_task(self, document_id: uuid.UUID, storage_url: str):
    """Phase 1 — OCR only. Leaves the document awaiting issuer review."""
    try:
        run_async(
            extract_document_job(
                document_id=document_id,
                storage_url=storage_url,
            )
        )
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(
    name="document.enrich",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def enrich_document_task(self, document_id: uuid.UUID):
    """Phase 2 — chunk, embed and analyze the text the issuer approved."""
    from app.infrastructure.nlp.analysis import LLMAnalysisProvider
    from app.infrastructure.nlp.chunking import build_chunker
    from app.infrastructure.nlp.embeddings import build_embedder

    try:
        run_async(
            enrich_document_job(
                document_id=document_id,
                analyzer=LLMAnalysisProvider(),
                chunker=build_chunker(),
                embedder=build_embedder(),
            )
        )
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(name="document.sync_draft_comments")
def sync_draft_comments_task():
    """Mirror Google Docs comments for every draft still being prepared."""
    from app.workers.sync_draft_comments import sync_due_drafts

    run_async(sync_due_drafts())
