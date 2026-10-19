"""Handing documents to the processing pipeline without losing them.

A state change that needs a job (finalize → OCR, approve → enrichment)
records a ProcessingDispatch in its own transaction, so the promise of work
commits with the state or not at all. The dispatch is then sent to the
broker; if the broker is down it stays pending and the beat task resends it.
A job can therefore arrive more than once — the workers skip work already
done.
"""

import uuid
from datetime import datetime, timezone

from app.infrastructure.observability.logging_setup import log
from app.infrastructure.queue import DocumentQueue
from sqlalchemy.ext.asyncio import AsyncSession

from .constants import JOB_EXTRACT
from .models import ProcessingDispatch
from .repository import DocumentRepository


class DocumentDispatcher:
    def __init__(self, session: AsyncSession, queue: DocumentQueue) -> None:
        self.session = session
        self.queue = queue
        self.repo = DocumentRepository(session)

    async def record(self, document_id: uuid.UUID, job: str) -> ProcessingDispatch:
        """Promise a job. Does not commit: the caller commits it with its state change."""
        dispatch = ProcessingDispatch(document_id=document_id, job=job)
        await self.repo.add(dispatch)
        return dispatch

    async def send(self, dispatch: ProcessingDispatch) -> bool:
        """Hand a committed dispatch to the broker; True once the broker took it.

        Never raises: a refused send is recorded on the dispatch and left for
        the retry task.
        """
        try:
            await self._enqueue(dispatch)
        except Exception as e:
            dispatch.attempts += 1
            dispatch.last_error = f"{type(e).__name__}: {e}"
            await self.session.commit()
            log.error(
                "document.dispatch.failed",
                document_id=str(dispatch.document_id),
                job=dispatch.job,
                attempts=dispatch.attempts,
                error=dispatch.last_error,
            )
            return False

        dispatch.dispatched_at = datetime.now(timezone.utc)
        await self.session.commit()
        log.info(
            "document.dispatch.sent",
            document_id=str(dispatch.document_id),
            job=dispatch.job,
        )
        return True

    async def _enqueue(self, dispatch: ProcessingDispatch) -> None:
        if dispatch.job != JOB_EXTRACT:
            await self.queue.enqueue_enrich_document(document_id=dispatch.document_id)
            return

        document = await self.repo.get_by_id(dispatch.document_id)
        await self.session.refresh(document, ["signed_copy"])
        copy = document.signed_copy
        await self.queue.enqueue_process_document(
            document_id=document.id,
            user_id=document.user_id,
            storage_url=copy.storage_url,
            content_type=copy.content_type,
            document_hash=copy.sha256,
        )
