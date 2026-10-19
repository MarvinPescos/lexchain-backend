"""Scheduled retry of Drive permissions a removed party still holds.

Each pending revocation runs in its own session, so one lawyer's revoked
Google access or a Drive error cannot stop the rest.
"""

import uuid

from sqlalchemy import select

from app.features.documents.models import DraftRevocation
from app.features.documents.workflow.drafting import DocumentDrafting
from app.features.google.service import GoogleConnectionService
from app.infrastructure.database import AsyncSessionLocal
from app.infrastructure.drafts import build_draft_workspace
from app.infrastructure.google import build_google_oauth
from app.infrastructure.observability.logging_setup import log


async def pending_revocation_ids() -> list[uuid.UUID]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DraftRevocation.id).order_by(DraftRevocation.created_at)
        )
        return list(result.scalars().all())


async def retry_one(revocation_id: uuid.UUID) -> bool:
    async with AsyncSessionLocal() as session:
        revocation = await session.get(DraftRevocation, revocation_id)
        if revocation is None:
            return True
        drafting = DocumentDrafting(
            session,
            build_draft_workspace(),
            GoogleConnectionService(session, build_google_oauth()),
        )
        return await drafting.revoke(revocation)


async def retry_pending_revocations() -> int:
    ids = await pending_revocation_ids()
    revoked = 0
    for revocation_id in ids:
        revoked += await retry_one(revocation_id)
    log.info("worker.draft_revoke.done", pending=len(ids), revoked=revoked)
    return revoked
