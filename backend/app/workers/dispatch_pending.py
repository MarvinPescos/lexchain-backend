"""Scheduled resend of processing jobs the broker has not accepted yet.

A dispatch is normally sent right after the commit that records it; this
picks up the ones whose send failed (broker down) or never ran (process died
between commit and send). Each runs in its own session and row lock, so two
beat runs cannot send the same dispatch at once.

It also reclaims jobs the broker took but no worker finished. A task that dies
before its body runs (a worker on older code rejecting the arguments) neither
retries nor marks the document FAILED, so the document would wait forever.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.celery_tasks import handle_document_processed
from app.core import settings
from app.features.documents.constants import (
    LIFECYCLE_SIGNED,
    STATUS_ENRICHING,
    STATUS_FAILED,
    STATUS_QUEUED,
)
from app.features.documents.dependencies import get_document_queue
from app.features.documents.dispatch import DocumentDispatcher
from app.features.documents.models import Document, ProcessingDispatch
from app.infrastructure.database import AsyncSessionLocal
from app.infrastructure.observability.logging_setup import log

# Leave a fresh dispatch to the request that recorded it: it is sent right
# after its commit, and resending it as well would only duplicate the job.
GRACE = timedelta(seconds=60)

# The statuses a document waits in between a job being sent and a worker
# picking it up.
WAITING_STATUSES = (STATUS_QUEUED, STATUS_ENRICHING)
# Resends of a stalled job before the document is marked FAILED, where the
# lawyer can see it and retry.
MAX_STALL_RESENDS = 3


async def pending_dispatch_ids() -> list[uuid.UUID]:
    cutoff = datetime.now(timezone.utc) - GRACE
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ProcessingDispatch.id)
            .where(
                ProcessingDispatch.dispatched_at.is_(None),
                ProcessingDispatch.created_at < cutoff,
            )
            .order_by(ProcessingDispatch.created_at)
        )
        return list(result.scalars().all())


async def send_one(dispatch_id: uuid.UUID) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ProcessingDispatch)
            .where(
                ProcessingDispatch.id == dispatch_id,
                ProcessingDispatch.dispatched_at.is_(None),
            )
            .with_for_update(skip_locked=True)
        )
        dispatch = result.scalar_one_or_none()
        if dispatch is None:
            return False
        return await DocumentDispatcher(session, get_document_queue()).send(dispatch)


async def stalled_dispatch_ids() -> list[uuid.UUID]:
    """Each waiting document's newest job, if it was sent too long ago.

    A delayed job is safe to resend: the workers skip work already done.
    Only SIGNED documents: processing finishes before finalize.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.DISPATCH_STALL_SECONDS
    )
    async with AsyncSessionLocal() as session:
        newest = (
            select(
                ProcessingDispatch.id,
                ProcessingDispatch.document_id,
                ProcessingDispatch.dispatched_at,
            )
            .distinct(ProcessingDispatch.document_id)
            .order_by(
                ProcessingDispatch.document_id, ProcessingDispatch.created_at.desc()
            )
            .subquery()
        )
        result = await session.execute(
            select(newest.c.id)
            .join(Document, Document.id == newest.c.document_id)
            .where(
                newest.c.dispatched_at < cutoff,
                Document.status.in_(WAITING_STATUSES),
                Document.lifecycle == LIFECYCLE_SIGNED,
            )
        )
        return list(result.scalars().all())


async def reclaim_one(dispatch_id: uuid.UUID) -> None:
    """Put a stalled job back for sending, or fail it once resends run out."""
    async with AsyncSessionLocal() as session:
        dispatch = await session.scalar(
            select(ProcessingDispatch)
            .where(
                ProcessingDispatch.id == dispatch_id,
                ProcessingDispatch.dispatched_at.is_not(None),
            )
            .with_for_update(skip_locked=True)
        )
        if dispatch is None:
            return
        # Locked and rechecked: a worker may have picked the job up since.
        document = await session.scalar(
            select(Document)
            .where(
                Document.id == dispatch.document_id,
                Document.status.in_(WAITING_STATUSES),
            )
            .with_for_update()
        )
        if document is None:
            return

        if dispatch.attempts < MAX_STALL_RESENDS:
            dispatch.dispatched_at = None
            dispatch.attempts += 1
            await session.commit()
            log.warning(
                "worker.dispatch.stalled",
                document_id=str(document.id),
                job=dispatch.job,
                attempts=dispatch.attempts,
            )
            return

        document.status = STATUS_FAILED
        dispatch.last_error = (
            f"No worker finished the job after {dispatch.attempts} resends"
        )
        await session.commit()
        log.error(
            "worker.dispatch.abandoned",
            document_id=str(document.id),
            job=dispatch.job,
            attempts=dispatch.attempts,
        )
        handle_document_processed.delay(
            user_id=str(document.user_id),
            document_title=document.file_name,
            document_id=str(document.id),
            status="failed",
        )


async def dispatch_pending() -> int:
    for dispatch_id in await stalled_dispatch_ids():
        await reclaim_one(dispatch_id)

    ids = await pending_dispatch_ids()
    sent = 0
    for dispatch_id in ids:
        sent += await send_one(dispatch_id)
    log.info("worker.dispatch.done", pending=len(ids), sent=sent)
    return sent
