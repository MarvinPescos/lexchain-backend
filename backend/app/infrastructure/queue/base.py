import uuid
from typing import Protocol

# No-op first
class DocumentQueue(Protocol):
    async def enqueue_process_document(
        self,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
        storage_url: str,
        content_type: str,
        document_hash: str,
        signed_copy_id: uuid.UUID,
    ) -> None:
        """Queue OCR of one signed copy; the job does nothing once it is replaced."""
        ...

    async def enqueue_enrich_document(
        self,
        document_id: uuid.UUID,
        signed_copy_id: uuid.UUID,
    ) -> None:
        """Queue chunk/embed/analysis once the issuer has approved the text."""
        ...