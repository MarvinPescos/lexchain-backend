"""Scheduled sync of draft comments from Google Docs.

Every document still PREPARING with a draft is synced in its own session, so
one lawyer's revoked Google access or a Drive error cannot stop the rest.
Ready-for-signature drafts are locked — nothing new can arrive — and are
synced once more by mark_ready itself.
"""

import uuid

from sqlalchemy import select

from app.features.documents.constants import LIFECYCLE_PREPARING
from app.features.documents.models import Document
from app.features.documents.workflow.discussion import DocumentDiscussion
from app.features.google.service import GoogleConnectionService
from app.infrastructure.database import AsyncSessionLocal
from app.infrastructure.drafts import build_draft_workspace
from app.infrastructure.google import build_google_oauth
from app.infrastructure.observability.logging_setup import log


async def due_document_ids() -> list[uuid.UUID]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Document.id).where(
                Document.lifecycle == LIFECYCLE_PREPARING,
                Document.draft_file_id.is_not(None),
            )
        )
        return list(result.scalars().all())


async def sync_one(document_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        try:
            document = await session.get(Document, document_id)
            if document is None or document.lifecycle != LIFECYCLE_PREPARING:
                return
            discussion = DocumentDiscussion(
                session,
                build_draft_workspace(),
                GoogleConnectionService(session, build_google_oauth()),
            )
            await discussion.sync(document)
            await session.commit()
            discussion.send_emails()
        except Exception as e:
            await session.rollback()
            log.error(
                "worker.draft_sync.failed",
                document_id=str(document_id),
                error=f"{type(e).__name__}: {e}",
            )


async def sync_due_drafts() -> int:
    ids = await due_document_ids()
    for document_id in ids:
        await sync_one(document_id)
    log.info("worker.draft_sync.done", documents=len(ids))
    return len(ids)
