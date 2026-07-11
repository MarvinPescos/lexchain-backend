import hashlib
import uuid
from pathlib import Path
from typing import List

from app.features.insights import DocumentInsightRepository
from app.features.extraction import DocumentExtractionRepository
from app.infrastructure.clients.supabase_storage import get_storage_client
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.queue import DocumentQueue
from app.shared.errors.exceptions import BadRequestError, NotFoundError
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Document
from .repository import DocumentRepository
from .schemas import (
    DocumentPartyListResponse,
    DocumentPartyResponse,
    DocumentResponse,
    DocumentUploadAcceptedResponse,
    RemovePartyResponse,
    VersionHistoryItem,
    VersionHistoryResponse,
)

MAX_FILE_SIZE = 60 * 1024 * 1024  # 10 MB

STATUS_UPLOADED = "UPLOADED"
STATUS_QUEUED = "QUEUED"


class DocumentService:
    """Service layer for document management."""

    def __init__(self, session: AsyncSession, queue: DocumentQueue) -> None:
        self.session = session
        self.queue = queue
        self.repo = DocumentRepository(session)
        self.insight_repo = DocumentInsightRepository(session)
        self.extraction_repo = DocumentExtractionRepository(session)

    # === Read ===

    async def get_all_documents(
        self, user_id: uuid.UUID, limit: int = 100, offset: int = 0
    ) -> List[Document]:
        """Return all documents scoped to the user."""
        log.info("document.fetch.all", user_id=str(user_id))
        return await self.repo.get_document_for_user(user_id, limit, offset)

    async def get_document(
        self, user_id: uuid.UUID, document_id: uuid.UUID
    ) -> DocumentResponse:
        """Return a single document with its analysis insight, raising 404 if not found."""
        document = await self.repo.get_one(user_id, document_id)

        if not document:
            is_party = await self.repo.is_party(user_id, document_id)
            if not is_party:
                raise NotFoundError("Document not found")
            document = await self.repo.get_by_id(document_id)

        insight = await self.insight_repo.get_by_document_id(document_id)

        log.info("document.get", user_id=str(user_id), document_id=str(document_id))

        return DocumentResponse(
            document_id=document.id,
            file_name=document.file_name,
            storage_url=document.storage_url,
            content_type=document.content_type,
            status=document.status,
            on_chain=document.on_chain,
            is_latest=document.is_latest,
            summary=insight.summary if insight else None,
            labels=(insight.labels or {}).get("items", []) if insight else [],
            entities=(insight.entities or {}).get("items", []) if insight else [],
            risk_flags=(insight.risk_flags or {}).get("items", []) if insight else [],
            created_at=document.created_at,
            updated_at=document.updated_at,
        )

    # === Write ===

    async def upload_document(
        self, user_id: uuid.UUID, file: UploadFile, file_name: str, book_id: uuid.UUID
    ) -> DocumentUploadAcceptedResponse:
        """Validate, store locally, persist metadata, and enqueue processing."""

        # 1. Read file bytes
        content = await file.read()
        # 2. Validate
        self._validate_file(file, content)
        # 3. Hash content for integrity / future deduplication
        document_hash = hashlib.sha256(content).hexdigest()

        # 4. Write to Supabase
        storage_path = await self._build_storage_path(
            file_bytes=content,
            user_id=user_id,
            folder="Docs",
            file_name=file_name,
            content_type=file.content_type,
        )

        # await self._write_file(storage_path, content)

        # 5. Persist metadata to the database
        try:
            document = await self.repo.create(
                user_id=user_id,
                book_id=book_id,
                file_name=file_name if file_name else file.file_name,
                content_type=file.content_type,
                storage_url=str(storage_path),
                document_hash=document_hash,
                status=STATUS_UPLOADED,
            )
            # Commit here so the row exists before enqueueing.
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            await get_storage_client().delete_docs(
                folder="Docs", file_name=file_name, user_id=user_id
            )
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

    async def update_document(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        file: UploadFile,
        file_name: str,
    ) -> DocumentUploadAcceptedResponse:
        """
        Create a new version of an existing document.

        - Superseded the old version (is_latest = False
        - New version link back via previous_document_id
        - Goes through the full upload pipeline
        """
        old_doc = await self.repo.get_one(user_id, document_id)
        if not old_doc:
            raise NotFoundError("Document not Found")

        content = await file.read()
        self._validate_file(file, content)
        document_hash = hashlib.sha256(content).hexdigest()

        storage_path = await self._build_storage_path(
            file_bytes=content,
            user_id=user_id,
            folder="Docs",
            file_name=file_name,
            content_type=file.content_type,
        )

        try:
            new_doc = await self.repo.create(
                user_id=user_id,
                file_name=file_name if file_name else file.filename,
                content_type=file.content_type,
                storage_url=str(storage_path),
                document_hash=document_hash,
                status=STATUS_UPLOADED,
                previous_document_id=old_doc.id,
                is_latest=True,
            )
            await self.repo.supersede(old_doc)
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            await get_storage_client().delete_docs(
                folder="Docs", file_name=file_name, user_id=user_id
            )
            raise
        try:
            await self.queue.enqueue_process_document(
                document_id=new_doc.id,
                user_id=user_id,
                storage_url=new_doc.storage_url,
                content_type=new_doc.content_type,
                document_hash=new_doc.document_hash,
            )
            new_doc = await self.repo.update(new_doc, status=STATUS_QUEUED)
            await self.session.commit()
        except Exception as enqueue_error:
            await self.session.rollback()
            log.error(
                "document.update.enqueue.failed",
                document_id=str(new_doc.id),
                user_id=str(user_id),
                error=str(enqueue_error),
            )
            return DocumentUploadAcceptedResponse(
                document_id=new_doc.id,
                status=STATUS_UPLOADED,
                message="Document update uploaded but queueing failed.",
            )
        log.info(
            "document.update",
            user_id=str(user_id),
            old_document_id=str(old_doc.id),
            new_document_id=str(new_doc.id),
            file_name=new_doc.file_name,
        )
        return DocumentUploadAcceptedResponse(
            document_id=new_doc.id,
            status=new_doc.status,
            message="Document update accepted for processing",
        )

    # async def get_version_history(
    #     self, user_id: uuid.UUID, document_id: uuid.UUID
    # ) -> VersionHistoryResponse:
    #     """Walk the linked list to return all versions of a document."""
    #     doc = await self.repo.get_one(user_id, document_id)
    #     is_party = await self.repo.is_party(user_id, document_id)
    #     if not doc and not is_party:
    #         raise NotFoundError("Document not found")
    #     chain = await self.repo.get_version_history(document_id)
    #     versions = [
    #         VersionHistoryItem(
    #             document_id=v.id,
    #             file_name=v.file_name,
    #             document_hash=v.document_hash,
    #             status=v.status,
    #             tx_hash=v.on_chain_record.tx_hash if v.on_chain_record else None,
    #             is_latest=v.is_latest,
    #             created_at=v.created_at,
    #         )
    #         for v in chain
    #     ]
    #     return VersionHistoryResponse(
    #         current_document_id=document_id,
    #         versions=versions,
    #         total_version=len(versions),
    #     )

    async def update_file_name(
        self, user_id: uuid.UUID, document_id: uuid.UUID, new_file_name: str
    ) -> Document:
        """Update a document's display name."""
        doc = await self.repo.get_one(user_id, document_id)
        if not doc:
            raise NotFoundError("Document was not found")

        updated = await self.repo.update(doc, file_name=new_file_name)

        log.info(
            "document.update.file.name",
            document_id=str(document_id),
            user_id=str(user_id),
            file_name=new_file_name,
        )
        return updated

    async def add_party(
        self,
        owner_id: uuid.UUID,
        document_id: uuid.UUID,
        email: str,
        role: str = "viewer",
    ):
        doc = await self.repo.get_one(owner_id, document_id)
        if not doc:
            raise NotFoundError("Document not found")

        from app.features.users.repository import UserRepository

        user_repo = UserRepository(self.session)
        user = await user_repo.get_by_email(email)
        if not user:
            raise NotFoundError("User with that email not found")

        if role in ["signer", "editor"] and user.role not in ["lawyer", "admin"]:
            raise BadRequestError(
                f"Cannot add as {role}. Only lawyers can be signers or editors.", 400
            )

        party = await self.repo.add_party(document_id, user.id, role, email)
        await self.session.refresh(party, ["user"])
        await self.session.commit()

        log.info(
            "document.party.add",
            document_id=str(document_id),
            owner_id=str(owner_id),
            email=email,
            role=role,
        )

        owner = await user_repo.get_by_id(doc.user_id)

        from app.celery_tasks import handle_party_added

        handle_party_added.delay(
            party_user_id=str(user.id),
            owner_name=f"{owner.f_name} {owner.l_name}",
            document_title=doc.file_name,
            document_id=str(document_id),
        )

        return party

    async def get_parties(self, user_id: uuid.UUID, document_id: uuid.UUID):
        doc = await self.repo.get_one(user_id, document_id)
        if not doc:
            is_party = await self.repo.is_party(user_id, document_id)
            if not is_party:
                raise NotFoundError("Document not found")
            doc = await self.repo.get_by_id(document_id)

        from app.features.users.repository import UserRepository

        user_repo = UserRepository(self.session)
        owner = await user_repo.get_by_id(doc.user_id)

        issuer = DocumentPartyResponse(
            id=owner.id,
            user_id=owner.id,
            email=owner.email,
            f_name=owner.f_name,
            l_name=owner.l_name,
            role="owner",
            # status=owner.status,
            created_at=doc.created_at,
        )

        parties = await self.repo.get_parties(document_id)

        return DocumentPartyListResponse(
            document_id=document_id,
            issuer=issuer,
            parties=[
                DocumentPartyResponse(
                    id=p.id,
                    user_id=p.user_id,
                    email=p.user.email,
                    f_name=p.user.f_name,
                    l_name=p.user.l_name,
                    role=p.role,
                    created_at=p.created_at,
                )
                for p in parties
            ],
        )

    async def remove_party(
        self, user_id: uuid.UUID, document_id: uuid.UUID, party_user_id: uuid.UUID
    ):
        doc = await self.repo.get_one(user_id, document_id)
        if not doc:
            raise NotFoundError("Document not found")

        removed = await self.repo.remove_party(document_id, party_user_id)
        if not removed:
            raise NotFoundError("Party not found on this document")
        await self.session.commit()
        log.info(
            "document.party.remove",
            document_id=str(document_id),
            owner_id=str(user_id),
            party_id=str(party_user_id),
        )

        return RemovePartyResponse(
            document_id=document_id,
            user_id=party_user_id,
            message="Party removed",
        )

    async def finalize_document(self, user_id: uuid.UUID, document_id: uuid.UUID, text: str):
        """Finalize the document (create a snapshot, anchor on blockchain, set finalized fields)"""



    # ========== helpers =============

    def _validate_file(self, file: UploadFile, content):
        """Helper for file validation"""

        # 2. Validate
        # 2.1 if empty
        if not content:
            raise BadRequestError("File is empty", 400)

        # 2.2 not supported types
        if file.content_type != "application/pdf":
            raise BadRequestError(
                f"Unsupported file type '{file.content_type}'. Allowed: pdf", 400
            )

        # 2.3 exceeds the maximum allowed size
        if len(content) > MAX_FILE_SIZE:
            raise BadRequestError(
                f"File exceeds the maximum allowed size of {MAX_FILE_SIZE // (1024 * 1024)} MB",
                400,
            )

    async def _build_storage_path(
        self,
        file_bytes: bytes,
        user_id: uuid.UUID,
        folder: str,
        file_name: str | None,
        content_type: str = "application/pdf",
    ) -> Path:
        try:
            document_url = await get_storage_client().upload_docs(
                file_bytes=file_bytes,
                user_id=user_id,
                folder=folder,
                file_name=f"{file_name}.",
                content_type=content_type,
            )

            return document_url

        except Exception as e:
            log.error(
                "gallery.storage.upload_failed",
                user_id=str(user_id),
                file_name=file_name,
                error=str(e),
            )

    # async def _write_file(self, storage_path: Path, content: bytes) -> None:
    #     def _write() -> None:
    #         storage_path.parent.mkdir(parents=True, exist_ok=True)
    #         storage_path.write_bytes(content)
    #     await asyncio.to_thread(_write)
