import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import List

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.errors.exceptions import BadRequestError, NotFoundError
from app.infrastructure.observability.logging_setup import log

from app.infrastructure.queue import DocumentQueue
from .repository import DocumentRepository
from .schemas import DocumentUploadAcceptedResponse, DocumentUploadResponse
from .models import Document

ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "text/plain",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

MAX_FILE_SIZE = 15 * 1024 * 1024  # 10 MB

UPLOAD_DIR = Path("uploads")

STATUS_UPLOADED = "UPLOADED"
STATUS_QUEUED = "QUEUED"


class DocumentService:
    """Service layer for document management."""

    def __init__(self, session: AsyncSession, queue: DocumentQueue) -> None:
        self.session = session
        self.queue = queue
        self.repo = DocumentRepository(session)

    # === Read ===

    async def get_all_documents(self, user_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[Document]:
        """Return all documents scoped to the user."""
        log.info("document.fetch.all", user_id=str(user_id))
        return await self.repo.get_many(user_id, limit, offset)

    async def get_document(self, user_id: uuid.UUID, document_id: uuid.UUID) -> Document:
        """Return a single document, raising 404 if not found."""
        document = await self.repo.get_one(user_id, document_id)

        if not document:
            raise NotFoundError("Document not found")

        log.info("document.get", user_id=str(user_id), document_id=str(document_id))
        return document

    # === Write ===

    async def upload_document(self, user_id: uuid.UUID, file: UploadFile) -> DocumentUploadAcceptedResponse:
        """Validate, store locally, persist metadata, and enqueue processing."""

        # 1. Read file bytes 
        content = await file.read()
        # 2. Validate
        self._validate_file(file, content)
        # 3. Hash content for integrity / future deduplication
        document_hash = hashlib.sha256(content).hexdigest()

        # 4. Write to local disk (non-blocking)
        storage_path = self._build_storage_path(user_id, file.filename)
        await self._write_file(storage_path, content)

        # 5. Persist metadata to the database
        try:
            document = await self.repo.create(
                user_id=user_id,
                file_name=file.filename,
                content_type=file.content_type,
                storage_url=str(storage_path),
                document_hash=document_hash,
                status=STATUS_UPLOADED,
            )
            # Commit here so the row exists before enqueueing.
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            await self._delete_file_if_exists(storage_path)
            raise

        # 6. Queue async processing and transition status.
        try:
            await self.queue.enqueue_process_document(
                document_id=document.id,
                user_id=user_id,
                storage_url=document.storage_url,
                content_type=document.content_type,
                document_hash=document.document_hash,
            )
            document = await self.repo.update(document, status=STATUS_QUEUED)
            await self.session.commit()

        except Exception as enqueue_error:
            await self.session.rollback()
            log.error(
                "document.enqueue.failed",
                document_id=str(document.id),
                user_id=str(user_id),
                error=str(enqueue_error),
            )
            return DocumentUploadAcceptedResponse(
                document_id=document.id,
                status=STATUS_UPLOADED,
                message="Document uploaded but queueing failed. Retry will be required.",
            )
            

        log.info(
            "document.upload",
            user_id=str(user_id),
            document_id=str(document.id),
            file_name=document.file_name,
            status=document.status,
        )

        return DocumentUploadAcceptedResponse(
            document_id=document.id,
            status=document.status,
            message="Document accepted for processing",
        )


    # ========== helpers =============

    async def _delete_file_if_exists(self, storage_path: Path) -> None:
        """Best-effort local cleanup used for DB-write failures."""

        def _delete() -> None:
            if storage_path.exists():
                storage_path.unlink()

        await asyncio.to_thread(_delete)

    def _validate_file(self, file: UploadFile, content):
        """Helper for file validation"""

        # 2. Validate
        # 2.1 if empty
        if not content:
            raise BadRequestError("File is empty", 400)

        # 2.2 not supported types
        if file.content_type not in ALLOWED_CONTENT_TYPES:
            raise BadRequestError(
                f"Unsupported file type '{file.content_type}'. "
                f"Allowed: {', '.join(sorted(ALLOWED_CONTENT_TYPES))}",
                400
            )

        # 2.3 exceeds the maximum allowed size
        if len(content) > MAX_FILE_SIZE:
            raise BadRequestError(
                f"File exceeds the maximum allowed size of {MAX_FILE_SIZE // (1024 * 1024)} MB",
                400
            )

    def _build_storage_path(self, user_id: uuid.UUID, filename: str | None) -> Path:
        user_upload_dir = UPLOAD_DIR / str(user_id)
        file_ext = Path(filename or "").suffix
        stored_name = f"{uuid.uuid4()}{file_ext}"
        return user_upload_dir / stored_name

    async def _write_file(self, storage_path: Path, content: bytes) -> None:
        def _write() -> None:
            storage_path.parent.mkdir(parents=True, exist_ok=True)
            storage_path.write_bytes(content)
        await asyncio.to_thread(_write)
