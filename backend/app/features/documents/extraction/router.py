"""OCR review routes: read, correct, LLM second opinion, approve.

Mounted under /documents by the documents router. Review only exists once a
document is finalized — OCR runs on the frozen signed copy.
"""

import uuid

from fastapi import APIRouter, Depends, Request, status

from app.infrastructure.security.dependencies import (
    AuthenticatedUser,
    require_auth,
    require_lawyer,
)

from .dependencies import get_extraction_review_service
from .schemas import (
    ApproveExtractionResponse,
    ExtractionReviewResponse,
    UpdateExtractionRequest,
)
from .service import ExtractionReviewService

router = APIRouter(tags=["Documents · OCR review"])


# ── OCR review: read ────────────────────────────────────────────────


@router.get(
    "/{document_id}/extraction",
    response_model=ExtractionReviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Get extracted text for review",
    description=(
        "Return the OCR blocks and the flags worth checking, so the issuer can "
        "jump to suspect text instead of reading the whole document. Each flag "
        "points at a block, and blocks carry page/bbox where the engine reports layout."
    ),
    responses={
        200: {"description": "Blocks and flags"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        404: {"description": "Document not found, or it has no extraction yet"},
    },
)
async def get_extraction_review(
    request: Request,
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_auth),
    service: ExtractionReviewService = Depends(get_extraction_review_service),
) -> ExtractionReviewResponse:
    return await service.get_review(current_user, document_id)


# ── OCR review: edit ────────────────────────────────────────────────


@router.patch(
    "/{document_id}/extraction",
    response_model=ExtractionReviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Correct extracted text",
    description=(
        "Save corrections to one or more blocks. Repeatable — this does not "
        "approve the document. Flags are recomputed so fixed problems disappear. "
        "The raw OCR text is never overwritten."
    ),
    responses={
        200: {"description": "Corrections saved, flags recomputed"},
        400: {"description": "Unknown block index, or nothing reviewable"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only the document owner may edit"},
        404: {"description": "Document not found, or it has no extraction yet"},
        409: {"description": "Already approved, or document is not awaiting review"},
    },
)
async def update_extraction(
    request: Request,
    document_id: uuid.UUID,
    body: UpdateExtractionRequest,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: ExtractionReviewService = Depends(get_extraction_review_service),
) -> ExtractionReviewResponse:
    return await service.apply_edits(current_user, document_id, body.edits)


# ── OCR review: LLM second opinion ──────────────────────────────────


@router.post(
    "/{document_id}/extraction/analyze",
    response_model=ExtractionReviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Ask the LLM for semantic inconsistencies",
    description=(
        "Adds LLM-found issues (name drift between clauses, contradictions, "
        "undefined terms, missing annexes) on top of the automatic checks. "
        "Optional — the rule-based flags already ran at extraction time, so this "
        "costs inference only when asked. Re-running replaces the previous LLM pass."
    ),
    responses={
        200: {"description": "Flags including the LLM pass"},
        400: {"description": "Nothing reviewable in this extraction"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only the document owner may run this"},
        404: {"description": "Document not found, or it has no extraction yet"},
        409: {"description": "Already approved, or document is not awaiting review"},
    },
)
async def analyze_extraction(
    request: Request,
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: ExtractionReviewService = Depends(get_extraction_review_service),
) -> ExtractionReviewResponse:
    return await service.run_llm_review(current_user, document_id)


# ── OCR review: approve ─────────────────────────────────────────────


@router.post(
    "/{document_id}/extraction/approve",
    response_model=ApproveExtractionResponse,
    status_code=status.HTTP_200_OK,
    summary="Approve extracted text",
    description=(
        "Freeze the reviewed text and queue chunking, embedding and analysis. "
        "Nothing is derived from the text before this point, so the search index "
        "is always built from what the issuer approved. The text is never "
        "anchored — the chain holds the hash of the signed PDF."
    ),
    responses={
        200: {"description": "Approved; enrichment queued"},
        400: {"description": "Approved text is empty"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only the document owner may approve"},
        404: {"description": "Document not found, or it has no extraction yet"},
        409: {"description": "Already approved, or document is not awaiting review"},
    },
)
async def approve_extraction(
    request: Request,
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: ExtractionReviewService = Depends(get_extraction_review_service),
) -> ApproveExtractionResponse:
    return await service.approve(current_user, document_id)
