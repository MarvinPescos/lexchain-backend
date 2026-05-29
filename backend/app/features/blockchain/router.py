from fastapi import APIRouter, Depends, status, Request
import uuid
from app.infrastructure.security.dependencies import AuthenticatedUser, require_auth, require_lawyer
from .service import BlockchainService
from .dependencies import get_blockchain_service
from .schemas import RecordResponse, OnChainVerificationResponse

router = APIRouter(prefix="/blockchain", tags=["Blockchain"])


@router.post(
    "/record/{document_id}",
    response_model=RecordResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a document on-chain",
    description="""
    Store the document hash on the LexChainNotary smart contract.
    - **Requirement**: Document must be in COMPLETED status.
    - The document's SHA256 hash is written to the blockchain.
    """,
    responses={
        201: {"description": "Document recorded on-chain"},
        400: {"description": "Document not ready or already recorded"},
        401: {"description": "Not authenticated"},
        403: {"description": "Not authorized"},
        404: {"description": "Document not found"},
    }
)
async def record_document(
    request: Request,
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: BlockchainService = Depends(get_blockchain_service),
) -> RecordResponse:
    return await service.record_document(current_user.user_id, document_id)
@router.get(
    "/verify/{document_id}",
    response_model=OnChainVerificationResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify a document's on-chain record",
    description="Query the smart contract for the document's stored hash and verify integrity.",
    responses={
        200: {"description": "On-chain record found"},
        401: {"description": "Not authenticated"},
        404: {"description": "No on-chain record found"},
    }
)
async def verify_document(
    request: Request,
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_auth),
    service: BlockchainService = Depends(get_blockchain_service),
) -> OnChainVerificationResponse:
    return await service.verify_document(current_user.user_id, document_id)