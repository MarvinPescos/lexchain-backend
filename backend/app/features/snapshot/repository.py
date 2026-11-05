import uuid
from collections.abc import Sequence

from app.features.documents.models import Document
from app.infrastructure.database import BaseRepository
from sqlalchemy import Row, select

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

    async def list_document_presence(
        self,
    ) -> Sequence[Row[tuple[uuid.UUID, str, uuid.UUID | None]]]:
        """Every document's ID and lifecycle, with its snapshot's ID if it has one."""
        result = await self.session.execute(
            select(Document.id, Document.lifecycle, DocumentSnapshot.id).outerjoin(
                DocumentSnapshot, DocumentSnapshot.document_id == Document.id
            )
        )
        return result.all()
