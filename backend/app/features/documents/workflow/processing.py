"""Retry: send a FAILED document back through the phase that failed.

A document fails after the worker used up its own retries (or hit a permanent
error). Once the cause is fixed — the OCR server is back, a provider key is
renewed — the owner retries it here. The phase is read from the extraction:
approved text means enrichment failed, anything else means OCR did.
"""

import uuid

from app.infrastructure.observability.logging_setup import log
from app.infrastructure.queue import DocumentQueue
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.errors.exceptions import ConflictError
from sqlalchemy.ext.asyncio import AsyncSession

from ..access import DocumentAccess
from ..constants import (
    ACTION_RETRY_PROCESSING,
    JOB_ENRICH,
    JOB_EXTRACT,
    STATUS_ENRICHING,
    STATUS_FAILED,
    STATUS_QUEUED,
)
from ..dispatch import DocumentDispatcher
from ..extraction.repository import DocumentExtractionRepository
from ..schemas import RetryProcessingResponse


class DocumentProcessing:
    def __init__(self, session: AsyncSession, queue: DocumentQueue) -> None:
        self.session = session
        self.dispatcher = DocumentDispatcher(session, queue)
        self.access = DocumentAccess(session)
        self.extraction_repo = DocumentExtractionRepository(session)

    async def retry(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> RetryProcessingResponse:
        document = await self.access.authorize(
            user, document_id, ACTION_RETRY_PROCESSING
        )
        # Locked so a double click queues one job, not two: the second request
        # waits, then finds the document no longer FAILED.
        await self.session.refresh(document, with_for_update=True)
        if document.status != STATUS_FAILED:
            raise ConflictError(
                f"Document is {document.status}; only a {STATUS_FAILED} document "
                "can be retried"
            )

        extraction = await self.extraction_repo.get_by_document_id(document_id)
        if extraction is not None and extraction.reviewed_at is not None:
            job, status = JOB_ENRICH, STATUS_ENRICHING
        else:
            job, status = JOB_EXTRACT, STATUS_QUEUED

        # Same outbox as intake/approve: the job commits with the status.
        document.status = status
        dispatch = await self.dispatcher.record(document_id, job)
        await self.session.commit()

        await self.dispatcher.send(dispatch)

        log.info(
            "document.processing.retry",
            document_id=str(document_id),
            user_id=str(user.user_id),
            job=job,
        )
        return RetryProcessingResponse(
            document_id=document_id,
            status=status,
            job=job,
            message=(
                "Text extraction has been queued again."
                if job == JOB_EXTRACT
                else "Chunking, embedding and analysis have been queued again."
            ),
        )
