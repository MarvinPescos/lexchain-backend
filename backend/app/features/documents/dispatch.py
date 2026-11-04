"""Handing documents to the processing pipeline without losing them.

A state change that needs a job (a signed copy lands → OCR, approve →
enrichment)
records a ProcessingDispatch in its own transaction, so the promise of work
commits with the state or not at all. The dispatch is then sent to the
broker; if the broker is down it stays pending and the beat task resends it.
A job can therefore arrive more than once — the workers skip work already
done.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select

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

    async def drop_pending(self, document_id: uuid.UUID) -> None:
        """Forget the document's jobs not yet sent; a newer one replaces them.

        Does not commit: the caller commits it with the job that replaces them.
        """
        await self.session.execute(
            delete(ProcessingDispatch).where(
                ProcessingDispatch.document_id == document_id,
                ProcessingDispatch.dispatched_at.is_(None),
            )
        )

    async def send(self, dispatch: ProcessingDispatch) -> bool:
        """Hand a committed dispatch to the broker; True once the broker took it.

        Never raises: a refused send is recorded on the dispatch and left for
        the retry task. A dispatch already sent, or dropped by a replace of the
        signed copy, is skipped.
        """
        # Locked first: a replace deletes pending dispatches, and must either
        # find this one already sent or delete it before it goes out.
        pending = await self.session.scalar(
            select(ProcessingDispatch.id)
            .where(
                ProcessingDispatch.id == dispatch.id,
                ProcessingDispatch.dispatched_at.is_(None),
            )
            .with_for_update()
        )
        if pending is None:
            log.info(
                "document.dispatch.superseded",
                document_id=str(dispatch.document_id),
                job=dispatch.job,
            )
            return False
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
        # The job names the copy that is current now. A replace before the
        # job runs makes it stale, and the worker then does nothing.
        document = await self.repo.get_by_id(dispatch.document_id)
        assert document is not None
        await self.session.refresh(document, ["signed_copy"])
        copy = document.signed_copy
        assert copy is not None
        if dispatch.job != JOB_EXTRACT:
            await self.queue.enqueue_enrich_document(
                document_id=document.id, signed_copy_id=copy.id
            )
            return

        await self.queue.enqueue_process_document(
            document_id=document.id,
            user_id=document.user_id,
            storage_url=copy.storage_url,
            content_type=copy.content_type,
            document_hash=copy.sha256,
            signed_copy_id=copy.id,
        )
