from app.core.config import settings
from app.infrastructure.database import get_db
from app.infrastructure.queue import (
    CeleryDocumentQueue,
    DocumentQueue,
    LoggingDocumentQueue,
)
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from .service import DocumentService


def get_document_queue() -> DocumentQueue:
    """Dependency injection for queue provider."""
    # Use CeleryDocumentQueue if broker URL is configured or we're in production
    if settings.CELERY_BROKER_URL or settings.ENVIRONMENT == "production":
        return CeleryDocumentQueue()
    return LoggingDocumentQueue()


def get_document_service(
    db: AsyncSession = Depends(get_db),
    queue: DocumentQueue = Depends(get_document_queue),
) -> DocumentService:
    """Dependency injection (provides DocumentService with database session)"""
    return DocumentService(db, queue)
