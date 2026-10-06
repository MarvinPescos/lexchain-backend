import uuid

from app.infrastructure.database import BaseRepository
from sqlalchemy import select

from .models import DocumentSnapshot


class DocumentSnaphotRepository(BaseRepository[DocumentSnapshot]):
    """Repository pattern for Document Snapshot"""

    def __init__(self, session):
        super().__init__(DocumentSnapshot, session)

    async def get_by_document_id(
        self, document_id: uuid.UUID
    ) -> DocumentSnapshot | None:
        """Fetch the frozen original for a document (one per document)."""
        result = await self.session.execute(
            select(DocumentSnapshot).where(
                DocumentSnapshot.document_id == document_id
            )
        )
        return result.scalar_one_or_none()
