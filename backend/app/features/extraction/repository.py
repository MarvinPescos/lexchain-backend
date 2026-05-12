from app.infrastructure.database.base_repository import BaseRepository

from .models import DocumentExtraction


class DocumentExtractionRepository(BaseRepository[DocumentExtraction]):
    """Repository for OCR / extraction outputs."""

    def __init__(self, session) -> None:
        super().__init__(DocumentExtraction, session)