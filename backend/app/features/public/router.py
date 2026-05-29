from fastapi import APIRouter, Depends, File, UploadFile, status
from .service import PublicVerifierService
from .dependencies import get_public_verifier
from .schemas import PublicVerifyResponse


router = APIRouter(prefix="/public", tags=["Public"])


@router.post(
    "/verify",
    response_model=PublicVerifyResponse,
    status_code=status.HTTP_200_OK,
    summary="Public document verifier",
    description="""
    Upload a PDF to verify if it matches a document recorded on-chain in the system.
    - No authentication required.
    - Extracts text via OCR, hashes it, and compares against the database.
    - If no exact text match, falls back to semantic similarity search.
    - Returns on-chain status (tx hash, notary address, timestamp) when found.
    """,
    responses={
        200: {"description": "Verification result"},
    }
)
async def verify_public(
    file: UploadFile = File(...),
    service: PublicVerifierService = Depends(get_public_verifier),
) -> PublicVerifyResponse:
    return await service.verify(file)