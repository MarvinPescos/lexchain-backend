"""Reading documents: the list, one document, its signed copies, its history.

Every read goes through `access.authorize`, so a party sees what the owner
sees and nobody else sees anything.
"""

import uuid
from typing import List

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.clients.supabase_storage import get_storage_client
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.errors.exceptions import NotFoundError

from .access import DocumentAccess
from .presenters import copy_response, document_response
from .repository import DocumentRepository
from .schemas import (
    DocumentHistoryResponse,
    DocumentResponse,
    DocumentUploadResponse,
    SignedCopyFileResponse,
    SignedCopyListResponse,
    StageChangeResponse,
)


# Long enough for a viewer to load the PDF, short enough that a link copied
# out of the app, or kept by someone since removed, soon stops working.
SIGNED_URL_TTL_SECONDS = 300


class DocumentQueries:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = DocumentRepository(session)
        self.access = DocumentAccess(session)

    async def list_documents(
        self,
        user_id: uuid.UUID,
        limit: int = 100,
        offset: int = 0,
        lifecycle_filter: str | None = None,
    ) -> List[DocumentUploadResponse]:
        """Every document the user owns or is an accepted party of.

        Each entry says whether it is the user's own document or one they were
        invited to, with the role and the date of the invitation, so a client can
        separate "my documents" from "shared with me" without a second call.
        """
        documents = await self.repo.get_document_for_user(
            user_id, limit, offset, lifecycle_filter
        )
        # One lookup for every invitation this user holds, rather than per document.
        party_rows = await self.repo.get_party_rows_for_user(user_id)

        log.info(
            "document.fetch.all",
            user_id=str(user_id),
            total=len(documents),
            shared=sum(1 for d in documents if d.id in party_rows),
        )

        results = []
        for d in documents:
            party = party_rows.get(d.id)
            is_owner = d.user_id == user_id
            results.append(
                DocumentUploadResponse(
                    id=d.id,
                    file_name=d.file_name,
                    lifecycle=d.lifecycle,
                    status=d.status,
                    on_chain=d.on_chain,
                    is_owner=is_owner,
                    my_role="owner"
                    if is_owner
                    else (party.role if party else "viewer"),
                    shared_at=None
                    if is_owner
                    else (party.created_at if party else None),
                    book_id=d.book_id,
                    doc_no=d.doc_no,
                    page_no=d.page_no,
                    created_at=d.created_at,
                )
            )
        return results

    async def get_document(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> DocumentResponse:
        """Return a single document with its analysis insight, raising 404 if not found."""
        document = await self.access.authorize(user, document_id)
        log.info(
            "document.get", user_id=str(user.user_id), document_id=str(document_id)
        )
        return await document_response(self.session, user, document)

    async def get_signed_copies(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> SignedCopyListResponse:
        """Every signed copy ever uploaded for this document, newest first."""
        document = await self.access.authorize(user, document_id)
        copies = await self.repo.get_signed_copies(document_id)
        return SignedCopyListResponse(
            document_id=document_id,
            copies=[copy_response(c, document) for c in copies],
        )

    async def get_signed_copy_file(
        self,
        user: AuthenticatedUser,
        document_id: uuid.UUID,
        copy_id: uuid.UUID | None = None,
    ) -> SignedCopyFileResponse:
        """A short-lived link to a signed copy's PDF (the current one by default).

        The only way to read the bytes: storage is private, so every read
        passes the same access check as the document itself.
        """
        document = await self.access.authorize(user, document_id)
        copy_id = copy_id or document.signed_copy_id
        copy = (
            await self.repo.get_signed_copy(document_id, copy_id) if copy_id else None
        )
        if copy is None:
            raise NotFoundError("Signed copy not found")

        url = await get_storage_client().signed_url(
            copy.storage_url, SIGNED_URL_TTL_SECONDS
        )
        log.info(
            "document.signed_copy.read",
            document_id=str(document_id),
            copy_id=str(copy.id),
            user_id=str(user.user_id),
        )
        return SignedCopyFileResponse(
            document_id=document_id,
            copy_id=copy.id,
            url=url,
            expires_in=SIGNED_URL_TTL_SECONDS,
        )

    async def get_history(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> DocumentHistoryResponse:
        """Every lifecycle action on this document, oldest first."""
        await self.access.authorize(user, document_id)
        changes = await self.repo.get_stage_changes(document_id)
        return DocumentHistoryResponse(
            document_id=document_id,
            changes=[StageChangeResponse.model_validate(c) for c in changes],
        )
