from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.infrastructure.database import get_db

from app.infrastructure.queue import DocumentQueue, LoggingDocumentQueue
from .service import DocumentService


def get_document_queue() -> DocumentQueue:
    """Dependency injection for queue provider."""
    return LoggingDocumentQueue()


def get_document_service(
    db: AsyncSession = Depends(get_db),
    queue: DocumentQueue = Depends(get_document_queue),
) -> DocumentService:
    """Dependency injection (provides DocumentService with database session)"""
    return DocumentService(db, queue)