from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
import uuid
from typing import Optional

from app.infrastructure.database.base_repository import BaseRepository
from app.infrastructure.observability.logging_setup import log
from .models import DocumentInsight

class DocumentInsightRepository(BaseRepository[DocumentInsight]):
    def __init__(self, session) -> None:
        super().__init__(DocumentInsight, session)

    async def get_by_document_id(self, document_id: uuid.UUID) -> Optional[DocumentInsight]:
        """Fetch the latest insight for a given document."""
        try:
            result = await self.session.execute(
                select(DocumentInsight)
                .where(DocumentInsight.document_id == document_id)
                .order_by(DocumentInsight.created_at.desc())
                .limit(1)
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
            raise