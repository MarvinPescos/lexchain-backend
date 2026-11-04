"""How a signed PDF gets into a document.

Path 1 opens a record before anything is signed; its signed copy is attached
later. Path 2 uploads an already signed copy in one step. Either way the file
is a SignedCopy written to its own storage path, the register entry is claimed
in the same transaction, and the copy is queued for processing in that same
commit — so the lawyer sees its risk clauses while it can still be replaced.
"""

import hashlib
import uuid

from app.infrastructure.clients.supabase_storage import (
    get_storage_client,
    object_path_from_url,
)
from app.features.chunks.repository import DocumentChunkRepository
from app.features.insights.repository import DocumentInsightRepository
from app.infrastructure.database import IntegrityConstraintError
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.queue import DocumentQueue
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.errors.exceptions import BadRequestError, ConflictError, NotFoundError
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from .. import lifecycle
from ..access import DocumentAccess
from ..constants import (
    ACTION_ATTACH_SIGNED_COPY,
    ACTION_CORRECT_ENTRY,
    ACTION_OPEN,
    ACTION_RENAME,
    ACTION_REPLACE_SIGNED_COPY,
    ACTION_UPLOAD_SIGNED,
    JOB_EXTRACT,
    STATUS_QUEUED,
)
from ..dispatch import DocumentDispatcher
from ..extraction.repository import DocumentExtractionRepository
from ..models import UQ_DOCUMENT_SIGNED_FILE, Document, ProcessingDispatch, SignedCopy
from ..presenters import document_response
from ..repository import DocumentRepository
from ..schemas import DocumentResponse

MAX_FILE_SIZE = 60 * 1024 * 1024  # 60 MB


class _SignedFile:
    """A signed PDF read, checked and hashed — ready to store."""

    def __init__(
        self, content: bytes, sha256: str, filename: str | None, content_type: str
    ) -> None:
        self.content = content
        self.sha256 = sha256
        self.filename = filename
        self.content_type = content_type


class DocumentIntake:
    def __init__(self, session: AsyncSession, queue: DocumentQueue) -> None:
        self.session = session
        self.dispatcher = DocumentDispatcher(session, queue)
        self.repo = DocumentRepository(session)
        self.access = DocumentAccess(session)

    # === Path 1: prepared with LexChain ===

    async def open_document(
        self, user: AuthenticatedUser, file_name: str
    ) -> DocumentResponse:
        """Open a document record before anything is signed. No file, no entry,
        no draft yet — the draft is created next, in Google Docs."""
        document = Document(user_id=user.user_id, file_name=file_name)
        change = lifecycle.transition(ACTION_OPEN, document, user.user_id, created=True)
        self.session.add(document)
        await self.session.flush()
        change.document_id = document.id
        await self.repo.add(change)
        await self.session.commit()

        log.info(
            "document.open", user_id=str(user.user_id), document_id=str(document.id)
        )
        return await document_response(self.session, user, document)

    async def attach_signed_copy(
        self,
        user: AuthenticatedUser,
        document_id: uuid.UUID,
        file: UploadFile,
        book_id: uuid.UUID,
        doc_no: int | None = None,
        page_no: int | None = None,
    ) -> DocumentResponse:
        """Path 1: the signed/notarized PDF comes back into the record it was prepared in."""
        document = await self.access.authorize(
            user, document_id, ACTION_ATTACH_SIGNED_COPY
        )

        from app.features.books.service import BookService

        books = BookService(self.session)
        # Fast refusal before storage, so a rejected filing writes no bytes.
        await books.check_filing(user.user_id, book_id, doc_no, page_no)

        signed = await self._read_signed_file(file)
        await self._refuse_duplicate(user.user_id, signed.sha256)

        copy = await self._store_signed_copy(user, document, signed)
        try:
            # claim_entry locks the book row and does not commit, so the entry
            # and the signed copy commit together — or roll back together.
            entry = await books.claim_entry(user.user_id, book_id, doc_no, page_no)
            document.book_id = entry.book_id
            document.doc_no = entry.doc_no
            document.page_no = entry.page_no
            await self._make_current(document, copy)
            change = lifecycle.transition(
                ACTION_ATTACH_SIGNED_COPY, document, user.user_id
            )
            await self.repo.add(change)
            dispatch = await self._queue_processing(document)
            await self.session.commit()
        except Exception as e:
            await self._undo_store(copy)
            self._raise_if_duplicate_index(e)
            raise

        await self.dispatcher.send(dispatch)
        log.info(
            "document.signed_copy.attach",
            document_id=str(document_id),
            doc_no=document.doc_no,
            page_no=document.page_no,
        )
        return await document_response(self.session, user, document)

    # === Path 2: already signed before it reaches LexChain ===

    async def upload_signed(
        self,
        user: AuthenticatedUser,
        file: UploadFile,
        file_name: str,
        book_id: uuid.UUID,
        doc_no: int | None = None,
        page_no: int | None = None,
        confirm_new_record: bool = False,
    ) -> DocumentResponse:
        """Upload an already signed/notarized PDF: record + filing + signed copy.

        Ends in SIGNED, with the signed copy queued for processing.
        """
        # Wrong-path guard: the signed copy of a document prepared in LexChain
        # belongs on that record, not on a new one.
        if not confirm_new_record:
            waiting = await self.repo.list_ready_for_signature(user.user_id)
            if waiting:
                raise ConflictError(
                    "Documents are waiting for their signed copy. Attach this file "
                    "to one of them, or confirm it is a new document.",
                    details={
                        "reason": "READY_FOR_SIGNATURE_EXISTS",
                        "documents": [
                            {
                                "document_id": str(d.id),
                                "file_name": d.file_name,
                                "parties": [p.email for p in d.parties],
                            }
                            for d in waiting
                        ],
                    },
                )

        from app.features.books.service import BookService

        books = BookService(self.session)
        await books.check_filing(user.user_id, book_id, doc_no, page_no)

        signed = await self._read_signed_file(file)
        await self._refuse_duplicate(user.user_id, signed.sha256)

        document = Document(
            user_id=user.user_id,
            file_name=file_name or (file.filename or "Untitled"),
        )
        change = lifecycle.transition(
            ACTION_UPLOAD_SIGNED, document, user.user_id, created=True
        )
        self.session.add(document)
        await self.session.flush()
        change.document_id = document.id

        copy = await self._store_signed_copy(user, document, signed)
        try:
            entry = await books.claim_entry(user.user_id, book_id, doc_no, page_no)
            document.book_id = entry.book_id
            document.doc_no = entry.doc_no
            document.page_no = entry.page_no
            await self._make_current(document, copy)
            await self.repo.add(change)
            dispatch = await self._queue_processing(document)
            await self.session.commit()
        except Exception as e:
            await self._undo_store(copy)
            self._raise_if_duplicate_index(e)
            raise

        await self.dispatcher.send(dispatch)
        log.info(
            "document.upload",
            user_id=str(user.user_id),
            document_id=str(document.id),
            doc_no=document.doc_no,
            page_no=document.page_no,
        )
        return await document_response(self.session, user, document)

    # === Corrections before finalize ===

    async def rename(
        self, user: AuthenticatedUser, document_id: uuid.UUID, file_name: str
    ) -> DocumentResponse:
        """Change the display name. Blocked once finalized or cancelled."""
        document = await self.access.authorize(user, document_id, ACTION_RENAME)
        document.file_name = file_name
        await self.session.commit()
        log.info("document.rename", document_id=str(document_id), file_name=file_name)
        return await document_response(self.session, user, document)

    async def replace_signed_copy(
        self,
        user: AuthenticatedUser,
        document_id: uuid.UUID,
        file: UploadFile,
        reason: str,
    ) -> DocumentResponse:
        """Swap in the right signed copy before finalize. The previous one is kept.

        What was derived from the previous copy — extraction, chunks, insights —
        is cleared, and the new copy is processed from the start. A job still
        running for the previous copy cannot write over it: the worker commits
        under the same row lock taken here, and only for the current copy.
        """
        if not reason or not reason.strip():
            raise BadRequestError("Give the reason the signed copy is being replaced")

        document = await self.access.authorize(
            user, document_id, ACTION_REPLACE_SIGNED_COPY
        )
        # Serializes with the worker's commits and with a concurrent replace.
        await self.session.refresh(document, with_for_update=True)
        lifecycle.ensure(ACTION_REPLACE_SIGNED_COPY, document)

        signed = await self._read_signed_file(file)
        if signed.sha256 == document.document_hash:
            raise ConflictError("This is the same file as the current signed copy")
        await self._refuse_duplicate(user.user_id, signed.sha256)

        copy = await self._store_signed_copy(
            user, document, signed, reason=reason.strip()
        )
        try:
            await self._make_current(document, copy)
            change = lifecycle.transition(
                ACTION_REPLACE_SIGNED_COPY, document, user.user_id
            )
            await self.repo.add(change)
            await self._clear_processing(document)
            dispatch = await self._queue_processing(document)
            await self.session.commit()
        except Exception as e:
            await self._undo_store(copy)
            self._raise_if_duplicate_index(e)
            raise

        await self.dispatcher.send(dispatch)
        log.info("document.signed_copy.replace", document_id=str(document_id))
        return await document_response(self.session, user, document)

    async def correct_entry(
        self,
        user: AuthenticatedUser,
        document_id: uuid.UUID,
        doc_no: int,
        page_no: int,
        book_id: uuid.UUID | None = None,
    ) -> DocumentResponse:
        """Fix a mistyped Doc. No. / Page No. (or book) before finalize."""
        document = await self.access.authorize(user, document_id, ACTION_CORRECT_ENTRY)
        target_book = book_id or document.book_id
        if target_book is None:
            raise NotFoundError("Book not found")

        from app.features.books.service import BookService

        books = BookService(self.session)
        await books.check_filing(user.user_id, target_book, doc_no, page_no)

        try:
            # Release the current entry first so it does not count as taken.
            document.book_id = document.doc_no = document.page_no = None
            await self.session.flush()

            entry = await books.claim_entry(user.user_id, target_book, doc_no, page_no)
            document.book_id = entry.book_id
            document.doc_no = entry.doc_no
            document.page_no = entry.page_no
            change = lifecycle.transition(ACTION_CORRECT_ENTRY, document, user.user_id)
            await self.repo.add(change)
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

        log.info("document.entry.correct", document_id=str(document_id), doc_no=doc_no)
        return await document_response(self.session, user, document)

    # ========== helpers =============

    async def _read_signed_file(self, file: UploadFile) -> _SignedFile:
        # One byte past the limit is enough to know it is too big; never buffer more.
        if file.size is not None and file.size > MAX_FILE_SIZE:
            self._refuse_oversized()
        content = await file.read(MAX_FILE_SIZE + 1)
        self._validate_file(file, content)
        return _SignedFile(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            filename=file.filename,
            content_type=file.content_type or "application/pdf",
        )

    def _validate_file(self, file: UploadFile, content: bytes) -> None:
        """A signed copy must be a non-empty PDF within the size limit."""
        if not content:
            raise BadRequestError("File is empty")

        if file.content_type != "application/pdf" or not content.startswith(b"%PDF-"):
            raise BadRequestError(
                f"Unsupported file type '{file.content_type}'. Allowed: pdf"
            )

        if len(content) > MAX_FILE_SIZE:
            self._refuse_oversized()

    def _refuse_oversized(self) -> None:
        raise BadRequestError(
            f"File exceeds the maximum allowed size of {MAX_FILE_SIZE // (1024 * 1024)} MB"
        )

    async def _refuse_duplicate(self, user_id: uuid.UUID, sha256: str) -> None:
        existing = await self.repo.find_current_signed_file(user_id, sha256)
        if existing:
            raise ConflictError(
                f"This file is already filed as '{existing.file_name}'.",
                details={"reason": "DUPLICATE_FILE", "document_id": str(existing.id)},
            )

    def _raise_if_duplicate_index(self, error: Exception) -> None:
        """A lost race on the duplicate index reads as the same error as the pre-check."""
        if isinstance(error, IntegrityConstraintError) and UQ_DOCUMENT_SIGNED_FILE in str(
            error.original_error
        ):
            raise ConflictError(
                "This file was just filed as another document.",
                details={"reason": "DUPLICATE_FILE"},
            )

    async def _store_signed_copy(
        self,
        user: AuthenticatedUser,
        document: Document,
        signed: _SignedFile,
        reason: str | None = None,
    ) -> SignedCopy:
        """Write the PDF to its own path and record it. Never overwrites."""
        copy_id = uuid.uuid4()
        # The display name is never part of the path, so no upload can land on
        # another copy's object.
        path = f"{document.user_id}/documents/{document.id}/{copy_id}.pdf"
        url = await get_storage_client().upload_new(
            file_path=path, file_bytes=signed.content, content_type=signed.content_type
        )
        copy = SignedCopy(
            id=copy_id,
            document_id=document.id,
            storage_url=url,
            sha256=signed.sha256,
            content_type=signed.content_type,
            size_bytes=len(signed.content),
            original_filename=signed.filename,
            uploaded_by=user.user_id,
            replaced_reason=reason,
        )
        await self.repo.add(copy)
        return copy

    async def _undo_store(self, copy: SignedCopy) -> None:
        url = copy.storage_url
        await self.session.rollback()
        await get_storage_client().delete_path(object_path_from_url(url))

    async def _make_current(self, document: Document, copy: SignedCopy) -> None:
        document.signed_copy_id = copy.id
        document.document_hash = copy.sha256
        await self.session.flush()

    async def _queue_processing(self, document: Document) -> ProcessingDispatch:
        """Promise OCR of the current copy. Not committed: the caller commits it
        with the copy, so a broker outage delays processing but never loses it."""
        document.status = STATUS_QUEUED
        return await self.dispatcher.record(document.id, JOB_EXTRACT)

    async def _clear_processing(self, document: Document) -> None:
        """Drop what was derived from the previous copy, and its unsent jobs."""
        await DocumentChunkRepository(self.session).delete_for_document(document.id)
        await DocumentInsightRepository(self.session).delete_for_document(document.id)
        extractions = DocumentExtractionRepository(self.session)
        extraction = await extractions.get_by_document_id(document.id)
        if extraction is not None:
            await extractions.delete(extraction)
        await self.dispatcher.drop_pending(document.id)
