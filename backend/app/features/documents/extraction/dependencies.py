from app.infrastructure.database import get_db
from app.infrastructure.queue import DocumentQueue
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..dependencies import get_document_queue
from .service import ExtractionReviewService


def get_extraction_review_service(
    db: AsyncSession = Depends(get_db),
    queue: DocumentQueue = Depends(get_document_queue),
) -> ExtractionReviewService:
    """Dependency injection for the issuer review service."""
    return ExtractionReviewService(db, queue)
