import uuid
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from typing import Optional
from app.infrastructure.database.base_repository import BaseRepository
from app.infrastructure.observability.logging_setup import log


from .models import DocumentExtraction


class DocumentExtractionRepository(BaseRepository[DocumentExtraction]):
    """Repository for OCR / extraction outputs."""

    def __init__(self, session) -> None:
        super().__init__(DocumentExtraction, session)

    async def get_by_document_id(self, document_id: uuid.UUID) -> Optional[DocumentExtraction]:
        """Fetch extraction for the given document"""

        try:
            result = await self.session.execute(
                select(DocumentExtraction).
                where(DocumentExtraction.document_id == document_id)
            )
            return result.scalar_one_or_none()
        except SQLAlchemyError as e:
            log.error(
                "database.error",
                model="DocumentInsight",
                operation="get_by_document_id",
                document_id=str(document_id),
                error=str(e),
            )
