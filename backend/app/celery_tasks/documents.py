import uuid

import app.all_models  # noqa: F401 — registers all SQLAlchemy models for the worker process
from app.celery_app import celery_app
from app.celery_tasks.runner import run_async
from app.workers.process_documents import enrich_document_job, extract_document_job


def _is_final_attempt(task) -> bool:
    """The job marks the document FAILED only when no retry is left.

    Before that it raises transient errors, and `self.retry` below schedules
    the next attempt.
    """
    return task.request.retries >= task.max_retries


@celery_app.task(
    name="document.extract",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def extract_document_task(
    self,
    document_id: uuid.UUID,
    storage_url: str,
    signed_copy_id: uuid.UUID | None = None,
):
    """Phase 1 — OCR only. Leaves the document awaiting issuer review.

    `signed_copy_id` is None only for messages queued before it was sent.
    """
    try:
        run_async(
            extract_document_job(
                document_id=document_id,
                storage_url=storage_url,
                signed_copy_id=signed_copy_id,
                final_attempt=_is_final_attempt(self),
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
def enrich_document_task(
    self, document_id: uuid.UUID, signed_copy_id: uuid.UUID | None = None
):
    """Phase 2 — chunk, embed and analyze the text the issuer approved."""
    from app.infrastructure.nlp.analysis import LLMAnalysisProvider
    from app.infrastructure.nlp.chunking import build_chunker
    from app.infrastructure.nlp.embeddings import build_embedder

    try:
        run_async(
            enrich_document_job(
                document_id=document_id,
                signed_copy_id=signed_copy_id,
                analyzer=LLMAnalysisProvider(),
                chunker=build_chunker(),
                embedder=build_embedder(),
                final_attempt=_is_final_attempt(self),
            )
        )
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(name="document.sync_draft_comments")
def sync_draft_comments_task():
    """Mirror Google Docs comments for every draft still being prepared."""
    from app.workers.sync_draft_comments import sync_due_drafts

    run_async(sync_due_drafts())


@celery_app.task(name="document.retry_draft_revocations")
def retry_draft_revocations_task():
    """Take back Drive access from removed parties that Google refused earlier."""
    from app.workers.retry_draft_revocations import retry_pending_revocations

    run_async(retry_pending_revocations())


@celery_app.task(name="document.dispatch_pending")
def dispatch_pending_task():
    """Resend processing jobs the broker refused when they were first sent."""
    from app.workers.dispatch_pending import dispatch_pending

    run_async(dispatch_pending())


@celery_app.task(name="document.reembed_chunks")
def reembed_chunks_task():
    """Embed chunks that have no vector, e.g. after switching embedding model."""
    from app.workers.reembed_chunks import reembed_missing_chunks

    run_async(reembed_missing_chunks())


@celery_app.task(name="document.verify_backups")
def verify_backups_task():
    """Check every archive against its anchored hash; problems are logged."""
    from app.workers.verify_backups import verify_backups

    run_async(verify_backups())
