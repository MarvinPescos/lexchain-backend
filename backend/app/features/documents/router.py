from fastapi import APIRouter, Depends, File, UploadFile, status, Request
from typing import List
import uuid

from app.infrastructure.security.dependencies import AuthenticatedUser, require_auth

from .service import DocumentService
from .dependencies import get_document_service
from .schemas import DocumentUploadAcceptedResponse, DocumentUploadResponse

router = APIRouter(prefix="/documents", tags=["Documents"])


@router.get(
    "/",
    response_model=List[DocumentUploadResponse],
    status_code=status.HTTP_200_OK,
    summary="List all documents",
    description="Return all documents belonging to the authenticated user.",
    responses={
        200: {"description": "List of documents"},
        401: {"description": "Not authenticated"},
    }
)
async def get_all_user_documents(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    current_user: AuthenticatedUser = Depends(require_auth),
    service: DocumentService = Depends(get_document_service),
) -> List[DocumentUploadResponse]:
    return await service.get_all_documents(
        current_user.user_id,
        limit,
        offset
    )


@router.get(
    "/{document_id}",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a document",
    description="Return a single document belonging to the authenticated user.",
    responses={
        200: {"description": "Document found"},
        401: {"description": "Not authenticated"},
        404: {"description": "Document not found"},
    }
)
async def get_user_document(
    request: Request,
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_auth),
    service: DocumentService = Depends(get_document_service),
) -> DocumentUploadResponse:
    return await service.get_document(current_user.user_id, document_id)


@router.post(
    "/upload",
    response_model=DocumentUploadAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document",
    description="""
    Upload a document file for the authenticated user.

    - **Supported types**: PDF, DOCX, DOC, TXT
    - **Max size**: 10 MB
    - The file is saved locally and indexed in the database.
    """,
    responses={
        202: {"description": "Document accepted for processing"},
        400: {"description": "Empty file, unsupported type, or file too large"},
        401: {"description": "Not authenticated"},
    }
)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(require_auth),
    service: DocumentService = Depends(get_document_service),
) -> DocumentUploadAcceptedResponse:
    return await service.upload_document(current_user.user_id, file)
