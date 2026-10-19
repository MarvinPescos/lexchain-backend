"""Scheduled resend of processing jobs the broker has not accepted yet.

A dispatch is normally sent right after the commit that records it; this
picks up the ones whose send failed (broker down) or never ran (process died
between commit and send). Each runs in its own session and row lock, so two
beat runs cannot send the same dispatch at once.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.features.documents.dependencies import get_document_queue
from app.features.documents.dispatch import DocumentDispatcher
from app.features.documents.models import ProcessingDispatch
from app.infrastructure.database import AsyncSessionLocal
from app.infrastructure.observability.logging_setup import log

# Leave a fresh dispatch to the request that recorded it: it is sent right
# after its commit, and resending it as well would only duplicate the job.
GRACE = timedelta(seconds=60)


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


async def dispatch_pending() -> int:
    ids = await pending_dispatch_ids()
    sent = 0
    for dispatch_id in ids:
        sent += await send_one(dispatch_id)
    log.info("worker.dispatch.done", pending=len(ids), sent=sent)
    return sent
