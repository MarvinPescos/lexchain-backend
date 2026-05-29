from fastapi import APIRouter, Depends, File, UploadFile, status, Request
from typing import List
import uuid

from app.infrastructure.security.dependencies import AuthenticatedUser, require_auth, require_lawyer

from .service import DocumentService
from .dependencies import get_document_service
from .schemas import (
    DocumentUploadAcceptedResponse, DocumentResponse, DocumentUploadResponse,
    RenameDocumentRequest,
    AddPartyRequest, DocumentPartyResponse,
    DocumentPartyListResponse, RemovePartyResponse,
    VersionHistoryResponse,
)

router = APIRouter(prefix="/documents", tags=["Documents"])


# ── List ────────────────────────────────────────────────────────────

@router.get(
    "/",
    response_model=List[DocumentUploadResponse],
    status_code=status.HTTP_200_OK,
    summary="List all documents",
    description="Return all latest document versions belonging to or shared with the authenticated user.",
    responses={
        200: {"description": "List of documents"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
    },
)
async def get_all_user_documents(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    current_user: AuthenticatedUser = Depends(require_auth),
    service: DocumentService = Depends(get_document_service),
) -> List[DocumentUploadResponse]:
    return await service.get_all_documents(
        current_user.user_id, limit, offset,
    )


# ── Detail ──────────────────────────────────────────────────────────

@router.get(
    "/{document_id}",
    response_model=DocumentResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a document",
    description="Return a single document if the user owns it or is a whitelisted party.",
    responses={
        200: {"description": "Document found"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        404: {"description": "Document not found or user has no access"},
    },
)
async def get_user_document(
    request: Request,
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_auth),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    return await service.get_document(current_user.user_id, document_id)


# ── Upload ──────────────────────────────────────────────────────────

@router.post(
    "/upload",
    response_model=DocumentUploadAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document",
    description="Upload a PDF document. Only lawyers and admins may upload.",
    responses={
        202: {"description": "Document accepted for processing"},
        400: {"description": "Empty file, unsupported file type, or file exceeds size limit"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only lawyers may upload documents"},
    },
)
async def upload_document(
    request: Request,
    file_name: str,
    file: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: DocumentService = Depends(get_document_service),
) -> DocumentUploadAcceptedResponse:
    return await service.upload_document(current_user.user_id, file, file_name)


# ── Rename ──────────────────────────────────────────────────────────

@router.patch(
    "/{document_id}",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_200_OK,
    summary="Rename a document",
    description="Update the display name of a document. Only the document owner (lawyer) may rename.",
    responses={
        200: {"description": "Document renamed"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only the document owner may rename"},
        404: {"description": "Document not found"},
    },
)
async def rename_document(
    document_id: uuid.UUID,
    body: RenameDocumentRequest,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: DocumentService = Depends(get_document_service),
) -> DocumentUploadResponse:
    return await service.update_file_name(
        current_user.user_id, document_id, body.file_name,
    )


# ── Update (new version) ────────────────────────────────────────────

@router.post(
    "/{document_id}/update",
    response_model=DocumentUploadAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Update a document (new version)",
    description="Upload a new file as the next version of an existing document. Only the document owner may update.",
    responses={
        202: {"description": "Update accepted for processing"},
        400: {"description": "Empty file, unsupported file type, or file exceeds size limit"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only the document owner may update"},
        404: {"description": "Document not found"},
    },
)
async def update_document(
    request: Request,
    document_id: uuid.UUID,
    file_name: str,
    file: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: DocumentService = Depends(get_document_service),
) -> DocumentUploadAcceptedResponse:
    return await service.update_document(
        current_user.user_id, document_id, file, file_name,
    )


# ── Version history ─────────────────────────────────────────────────

@router.get(
    "/{document_id}/versions",
    response_model=VersionHistoryResponse,
    status_code=status.HTTP_200_OK,
    summary="Get version history",
    description="Return the full version chain for a document (newest → oldest). Accessible by owner or any party.",
    responses={
        200: {"description": "Version history"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        404: {"description": "Document not found or user has no access"},
    },
)
async def get_version_history(
    request: Request,
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_auth),
    service: DocumentService = Depends(get_document_service),
) -> VersionHistoryResponse:
    return await service.get_version_history(
        current_user.user_id, document_id,
    )


# ── Parties: list ───────────────────────────────────────────────────

@router.get(
    "/{document_id}/parties",
    response_model=DocumentPartyListResponse,
    status_code=status.HTTP_200_OK,
    summary="List document parties",
    description="Return all whitelisted parties for a document. Accessible by owner or any party.",
    responses={
        200: {"description": "List of parties"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        404: {"description": "Document not found or user has no access"},
    },
)
async def get_document_parties(
    request: Request,
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_auth),
    service: DocumentService = Depends(get_document_service),
) -> DocumentPartyListResponse:
    return await service.get_parties(
        current_user.user_id, document_id,
    )


# ── Parties: add ────────────────────────────────────────────────────

@router.post(
    "/{document_id}/parties",
    response_model=DocumentPartyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a party to a document",
    description="Whitelist a user by email to access this document. Only the document owner may add parties.",
    responses={
        201: {"description": "Party added"},
        400: {"description": "User does not exist, or role is invalid for this user"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only the document owner may add parties"},
        404: {"description": "Document not found"},
    },
)
async def add_document_party(
    request: Request,
    document_id: uuid.UUID,
    body: AddPartyRequest,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: DocumentService = Depends(get_document_service),
) -> DocumentPartyResponse:
    party = await service.add_party(
        current_user.user_id, document_id, body.email, body.role,
    )
    return DocumentPartyResponse(
        id=party.id,
        user_id=party.user_id,
        email=party.email,
        f_name=party.user.f_name if party.user else "",
        l_name=party.user.l_name if party.user else "",
        role=party.role,
        created_at=party.created_at,
    )


# ── Parties: remove ─────────────────────────────────────────────────

@router.delete(
    "/{document_id}/parties/{party_user_id}",
    response_model=RemovePartyResponse,
    status_code=status.HTTP_200_OK,
    summary="Remove a party from a document",
    description="Revoke a user's access to this document. Only the document owner may remove parties.",
    responses={
        200: {"description": "Party removed"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only the document owner may remove parties"},
        404: {"description": "Document or party not found"},
    },
)
async def remove_document_party(
    request: Request,
    document_id: uuid.UUID,
    party_user_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: DocumentService = Depends(get_document_service),
) -> RemovePartyResponse:
    return await service.remove_party(
        current_user.user_id, document_id, party_user_id,
    )