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
    ) -> None:
        ...