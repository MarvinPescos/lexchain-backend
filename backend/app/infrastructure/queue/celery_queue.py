import uuid


class CeleryDocumentQueue:
    async def enqueue_process_document(
        self,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
        storage_url: str,
        content_type: str,
        document_hash: str,
    ) -> None:
        from app.celery_tasks.documents import extract_document_task

        extract_document_task.delay(
            document_id=str(document_id),
            storage_url=storage_url,
        )

    async def enqueue_enrich_document(self, document_id: uuid.UUID) -> None:
        from app.celery_tasks.documents import enrich_document_task

        enrich_document_task.delay(document_id=str(document_id))
