import uuid
from datetime import datetime
from typing import List, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status

from app.features.blockchain.schemas import (
    DocumentVerificationResponse,
    IntegrityReportResponse,
    RecordResponse,
)
from app.infrastructure.security.dependencies import (
    AuthenticatedUser,
    require_auth,
    require_lawyer,
)

from .dependencies import (
    get_discussion,
    get_drafting,
    get_ecopy,
    get_finalization,
    get_intake,
    get_integrity_report,
    get_tamper_reports,
    get_processing,
    get_queries,
    get_restoration,
    get_sharing,
    get_verification,
)
from .extraction.router import router as extraction_router
from .queries import DocumentQueries
from .workflow.discussion import DocumentDiscussion
from .schemas import (
    AddPartyRequest,
    CorrectEntryRequest,
    DeclineEcopyRequest,
    EcopyRequestListResponse,
    EcopyRequestResponse,
    DocumentHistoryResponse,
    OpenDocumentRequest,
    SignedCopyFileResponse,
    SignedCopyListResponse,
    UpdateDocumentRequest,
    DocumentInvitationActionResponse,
    DocumentInvitationListResponse,
    DocumentPartyListResponse,
    DocumentPartyResponse,
    DocumentResponse,
    DraftCommentListResponse,
    DocumentUploadResponse,
    RegisterLookupResponse,
    RegisterReference,
    RemovePartyResponse,
    RestoreAllResponse,
    RestoreResponse,
    TamperReportListResponse,
    RetryProcessingResponse,
)
from .workflow.drafting import DocumentDrafting
from .workflow.ecopy import DocumentEcopy
from .workflow.finalization import DocumentFinalization
from .workflow.intake import DocumentIntake
from .workflow.processing import DocumentProcessing
from .workflow.restoration import DocumentRestoration
from .workflow.sharing import DocumentSharing, party_response
from .workflow.tamper_reports import TamperReports

# Swagger groups, in the order a document moves through them. Order and
# descriptions are set in main.py (`openapi_tags`).
TAG_DOCUMENTS = "Documents"
TAG_PREPARATION = "Documents · Preparation (Path 1)"
TAG_SIGNED_COPY = "Documents · Signed copy & register"
TAG_FINALIZE = "Documents · Finalize & verify"
TAG_SHARING = "Documents · Sharing"
TAG_ECOPY = "Documents · E-copy requests"

router = APIRouter(prefix="/documents")


# ── List ────────────────────────────────────────────────────────────


@router.get(
    "/",
    tags=[TAG_DOCUMENTS],
    response_model=List[DocumentUploadResponse],
    status_code=status.HTTP_200_OK,
    summary="List all documents",
    description="Every document belonging to or shared with the authenticated user, optionally filtered by lifecycle.",
    responses={
        200: {"description": "List of documents"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
    },
)
async def get_all_user_documents(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    lifecycle: str | None = Query(None, description="Filter by lifecycle stage"),
    current_user: AuthenticatedUser = Depends(require_auth),
    queries: DocumentQueries = Depends(get_queries),
) -> List[DocumentUploadResponse]:
    return await queries.list_documents(
        current_user.user_id,
        limit,
        offset,
        lifecycle,
    )


# ── Integrity report ────────────────────────────────────────────────
# Declared before /{document_id}: "verifications" is not a document id.


@router.get(
    "/verifications",
    tags=[TAG_FINALIZE],
    response_model=IntegrityReportResponse,
    status_code=status.HTTP_200_OK,
    summary="Integrity report: stored verification results",
    description=(
        "Every check made through GET /documents/{document_id}/verify on a "
        "document the lawyer owns, newest first. Range is on `verified_at` in "
        "UTC: `verified_from` inclusive, `verified_to` exclusive; a time without "
        "an offset is read as UTC; either may be omitted. `summary` counts every "
        "result in the range, not just the page. Reads stored verdicts only — "
        "nothing is re-verified. Parties to a document do not see its results."
    ),
    responses={
        200: {"description": "One page of results and the totals for the range"},
        400: {"description": "verified_from is not earlier than verified_to"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only lawyers have an integrity report"},
        422: {"description": "limit outside 1–100, negative offset, or a malformed time"},
    },
)
async def get_integrity_report_results(
    request: Request,
    verified_from: datetime | None = Query(
        None, description="Inclusive lower bound on verified_at (ISO 8601; UTC if no offset)"
    ),
    verified_to: datetime | None = Query(
        None, description="Exclusive upper bound on verified_at (ISO 8601; UTC if no offset)"
    ),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: AuthenticatedUser = Depends(require_lawyer),
    # Untyped: same circular import as get_verification.
    report=Depends(get_integrity_report),
) -> IntegrityReportResponse:
    return await report.for_owner(
        current_user.user_id, verified_from, verified_to, limit, offset
    )


# ── Tamper reports ──────────────────────────────────────
# Declared before /{document_id}: "tamper-reports" is not a document id.


@router.get(
    "/tamper-reports",
    tags=[TAG_FINALIZE],
    response_model=TamperReportListResponse,
    status_code=status.HTTP_200_OK,
    summary="Documents with a tamper problem still open",
    description=(
        "The lawyer's own documents that a check flagged and that haven't been "
        "fixed, newest first. Each is checked again now, so a fixed document "
        "drops off and `changes` is current: the PDF, the metadata, the snapshot "
        "row or the lifecycle. A document with no on-chain record isn't listed."
    ),
    responses={
        200: {"description": "The open reports"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only lawyers have tamper reports"},
    },
)
async def list_tamper_reports(
    current_user: AuthenticatedUser = Depends(require_lawyer),
    reports: TamperReports = Depends(get_tamper_reports),
) -> TamperReportListResponse:
    return await reports.list_open(current_user)


@router.post(
    "/tamper-reports/restore-all",
    tags=[TAG_FINALIZE],
    response_model=RestoreAllResponse,
    status_code=status.HTTP_200_OK,
    summary="Restore every open tamper report",
    description=(
        "Runs POST /documents/{document_id}/restore on each open report, one "
        "document at a time. A document that can't be restored goes in `failed` "
        "and the rest carry on."
    ),
    responses={
        200: {"description": "What was restored, and what failed"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only lawyers have tamper reports"},
    },
)
async def restore_all_tamper_reports(
    current_user: AuthenticatedUser = Depends(require_lawyer),
    reports: TamperReports = Depends(get_tamper_reports),
) -> RestoreAllResponse:
    return await reports.restore_all(current_user)


# ── E-copy requests: queues ─────────────────────────────────────────
# Declared before /{document_id}: "ecopy-requests" is not a document id.


@router.get(
    "/ecopy-requests",
    tags=[TAG_ECOPY],
    response_model=EcopyRequestListResponse,
    status_code=status.HTTP_200_OK,
    summary="Review queue: e-copy requests on my documents",
    description=(
        "Every e-copy request on a document the lawyer owns, newest first. "
        "Filter with `status=PENDING` for what still needs a decision. Requests "
        "on other lawyers' documents are never included."
    ),
    responses={
        200: {"description": "Requests on the lawyer's documents"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only lawyers review e-copy requests"},
    },
)
async def list_ecopy_review_queue(
    status_filter: Literal["PENDING", "APPROVED", "DECLINED"] | None = Query(
        None, alias="status"
    ),
    current_user: AuthenticatedUser = Depends(require_lawyer),
    ecopy: DocumentEcopy = Depends(get_ecopy),
) -> EcopyRequestListResponse:
    return await ecopy.list_for_owner(current_user, status_filter)


@router.get(
    "/ecopy-requests/mine",
    tags=[TAG_ECOPY],
    response_model=EcopyRequestListResponse,
    status_code=status.HTTP_200_OK,
    summary="My e-copy requests",
    description=(
        "Every e-copy request the caller made, newest first, on documents they "
        "still have access to — with decision and delivery status."
    ),
    responses={
        200: {"description": "The caller's requests"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
    },
)
async def list_my_ecopy_requests(
    current_user: AuthenticatedUser = Depends(require_auth),
    ecopy: DocumentEcopy = Depends(get_ecopy),
) -> EcopyRequestListResponse:
    return await ecopy.list_mine(current_user)


# ── Register lookup ─────────────────────────────────────────────────


@router.get(
    "/register-lookup",
    tags=[TAG_DOCUMENTS],
    response_model=RegisterLookupResponse,
    status_code=status.HTTP_200_OK,
    summary="Find a document by its register reference",
    description=(
        "Exact lookup by a complete notarial register reference: Doc. No., "
        "Page No., Book No. and Series year must all match. Searches every "
        "document the user owns or was accepted onto, filed in an open or "
        "closed book, whether or not it has been processed.\n\n"
        "Book numbers are per lawyer, so more than one document can match; "
        "each is returned. No match is an empty `results` list."
    ),
    responses={
        200: {"description": "Matching documents; empty if none"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        422: {"description": "A number is missing, not an integer, or out of range"},
    },
)
async def lookup_register_reference(
    doc_no: int = Query(..., ge=1, description="Doc. No. in the register"),
    page_no: int = Query(..., ge=1, description="Page No. in the register"),
    book_number: int = Query(
        ..., ge=1, le=1000, description="Book No. of the physical register"
    ),
    series_year: int = Query(..., ge=2000, description="Series year of the book"),
    current_user: AuthenticatedUser = Depends(require_auth),
    queries: DocumentQueries = Depends(get_queries),
) -> RegisterLookupResponse:
    return await queries.lookup_register_reference(
        current_user.user_id,
        RegisterReference(
            doc_no=doc_no,
            page_no=page_no,
            book_number=book_number,
            series_year=series_year,
        ),
    )


# ── Path 1: open a document ─────────────────────────────────────────


@router.post(
    "/",
    tags=[TAG_PREPARATION],
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Open a document (Path 1)",
    description=(
        "Open a document record before anything is signed. The working draft "
        "lives in Google Docs; the record starts PREPARING with no file and no "
        "register entry."
    ),
    responses={
        201: {"description": "Document opened"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only lawyers may open documents"},
    },
)
async def open_document(
    body: OpenDocumentRequest,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    intake: DocumentIntake = Depends(get_intake),
) -> DocumentResponse:
    return await intake.open_document(current_user, body.file_name)


# ── Path 2: upload an already signed document ───────────────────────


@router.post(
    "/upload",
    tags=[TAG_SIGNED_COPY],
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload an already signed document (Path 2)",
    description=(
        "Upload a signed/notarized PDF and file it in a register book. The "
        "document is SIGNED and its signed copy is queued for processing "
        "(`status` QUEUED): OCR, the lawyer's review of the text, then "
        "analysis, so its risk clauses are shown before finalize.\n\n"
        "`doc_no` / `page_no` are the Doc. No. and Page No. from the notarial "
        "block. Optional for an OPEN book — the next free entry is used. "
        "Required for a CLOSED book, where they are copied from the paper register.\n\n"
        "Refused with 409 `READY_FOR_SIGNATURE_EXISTS` while documents are "
        "waiting for their signed copy, unless `confirm_new_record=true`."
    ),
    responses={
        201: {"description": "Document filed, SIGNED"},
        400: {
            "description": "Empty file, unsupported file type, file exceeds size limit, or register numbers missing/out of range"
        },
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only lawyers may upload documents"},
        404: {"description": "Book not found"},
        409: {
            "description": "Same file already filed, Doc. No. taken, page full, or documents waiting for a signed copy"
        },
    },
)
async def upload_document(
    request: Request,
    file_name: str,
    book_id: uuid.UUID,
    doc_no: int | None = Query(None, ge=1, description="Doc. No. in the register"),
    page_no: int | None = Query(None, ge=1, description="Page No. in the register"),
    confirm_new_record: bool = Query(
        False,
        description="Confirm this is not the signed copy of a document already open",
    ),
    file: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(require_lawyer),
    intake: DocumentIntake = Depends(get_intake),
) -> DocumentResponse:
    return await intake.upload_signed(
        current_user, file, file_name, book_id, doc_no, page_no, confirm_new_record
    )


# ── Detail ──────────────────────────────────────────────────────────


@router.get(
    "/{document_id}",
    tags=[TAG_DOCUMENTS],
    response_model=DocumentResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a document",
    description="Return a single document, plus what the requesting user is permitted to do with it, if they own it or are a whitelisted party.",
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
    queries: DocumentQueries = Depends(get_queries),
) -> DocumentResponse:
    return await queries.get_document(current_user, document_id)


# ── Rename ──────────────────────────────────────────────────────────


@router.patch(
    "/{document_id}",
    tags=[TAG_DOCUMENTS],
    response_model=DocumentResponse,
    status_code=status.HTTP_200_OK,
    summary="Rename a document",
    description="Change the display name, until the document is finalized. Owner only.",
    responses={
        200: {"description": "Document renamed"},
        403: {"description": "Forbidden — only the document owner may rename"},
        404: {"description": "Document not found"},
        409: {"description": "Finalized or cancelled"},
    },
)
async def rename_document(
    document_id: uuid.UUID,
    body: UpdateDocumentRequest,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    intake: DocumentIntake = Depends(get_intake),
) -> DocumentResponse:
    return await intake.rename(current_user, document_id, body.file_name)


# ── Working draft (Google Docs) ─────────────────────────────────────


@router.post(
    "/{document_id}/draft",
    tags=[TAG_PREPARATION],
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create the working draft in Google Docs",
    description=(
        "Creates the draft in the lawyer's own Google Drive and shares it with "
        "every accepted client as a commenter (another lawyer on the document "
        "as an editor). Open `draft_url` in a new tab afterwards.\n\n"
        "Form fields. `source`: `blank` (new empty Doc), `template` (copy of "
        "`file_id`), `existing` (use `file_id` itself) or `upload` (`file` — "
        "say a Word file from the client — converted to a Doc). `file_id` comes "
        "from the Google Picker — see `GET /google/picker-token`. `file`: "
        "docx, doc, odt or rtf, at most 10 MB.\n\n"
        "Only while PREPARING, and only once per document."
    ),
    responses={
        201: {"description": "Draft created and shared"},
        400: {
            "description": "No file picked or chosen, the picked file is not a Google Doc, "
            "or the uploaded file is the wrong type, empty or too large"
        },
        403: {"description": "Forbidden — only the document owner may create the draft"},
        404: {"description": "Document not found"},
        409: {
            "description": "Not PREPARING, a draft already exists, or Google is not connected (`GOOGLE_NOT_CONNECTED`)"
        },
        502: {"description": "Google Drive is unreachable"},
    },
)
async def create_draft(
    document_id: uuid.UUID,
    source: Literal["blank", "template", "existing", "upload"] = Form("blank"),
    file_id: str | None = Form(
        None, description="Drive file id from the Google Picker, for template / existing"
    ),
    file: UploadFile | None = File(None, description="The file to convert, for upload"),
    current_user: AuthenticatedUser = Depends(require_lawyer),
    drafting: DocumentDrafting = Depends(get_drafting),
) -> DocumentResponse:
    return await drafting.create_draft(current_user, document_id, source, file_id, file)


# ── Draft comments ──────────────────────────────────────────────────


@router.get(
    "/{document_id}/comments",
    tags=[TAG_PREPARATION],
    response_model=DraftCommentListResponse,
    summary="Comments on the working draft",
    description=(
        "The comment threads from the Google Docs draft, as of the last sync. "
        "Kept after the draft is locked, as the record of how the text was agreed. "
        "Owner and accepted parties."
    ),
    responses={404: {"description": "Document not found"}},
)
async def list_draft_comments(
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_auth),
    discussion: DocumentDiscussion = Depends(get_discussion),
) -> DraftCommentListResponse:
    return await discussion.list_comments(current_user, document_id)


@router.post(
    "/{document_id}/comments/sync",
    tags=[TAG_PREPARATION],
    response_model=DraftCommentListResponse,
    summary="Pull the latest comments from Google Docs",
    description=(
        "Syncs right away instead of waiting for the scheduled sync, and "
        "notifies the people in each thread about anything new."
    ),
    responses={
        404: {"description": "Document not found"},
        409: {"description": "The lawyer's Google account is not connected"},
        502: {"description": "Google Drive is unreachable"},
    },
)
async def sync_draft_comments(
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_auth),
    discussion: DocumentDiscussion = Depends(get_discussion),
) -> DraftCommentListResponse:
    return await discussion.sync_now(current_user, document_id)


# ── Lifecycle actions ───────────────────────────────────────────────


@router.post(
    "/{document_id}/ready",
    tags=[TAG_PREPARATION],
    response_model=DocumentResponse,
    summary="Mark ready for signature",
    description=(
        "The text is agreed: the Google Docs draft is locked (clients can only "
        "read it) and it goes to print. Only from PREPARING. Comments are synced "
        "first, and this is refused with 409 `UNRESOLVED_COMMENTS` while any "
        "thread on the draft is open."
    ),
)
async def mark_ready(
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    drafting: DocumentDrafting = Depends(get_drafting),
) -> DocumentResponse:
    return await drafting.mark_ready(current_user, document_id)


@router.post(
    "/{document_id}/reopen",
    tags=[TAG_PREPARATION],
    response_model=DocumentResponse,
    summary="Back to preparing",
    description="Send a document that is ready for signature back to PREPARING. Clients can comment on the draft again.",
)
async def reopen(
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    drafting: DocumentDrafting = Depends(get_drafting),
) -> DocumentResponse:
    return await drafting.reopen(current_user, document_id)


@router.post(
    "/{document_id}/cancel",
    tags=[TAG_PREPARATION],
    response_model=DocumentResponse,
    summary="Cancel a document that was never signed",
    description="Only from PREPARING or READY_FOR_SIGNATURE. The record is kept.",
)
async def cancel(
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    drafting: DocumentDrafting = Depends(get_drafting),
) -> DocumentResponse:
    return await drafting.cancel(current_user, document_id)


# ── Signed copy ─────────────────────────────────────────────────────


@router.post(
    "/{document_id}/signed-copy",
    tags=[TAG_SIGNED_COPY],
    response_model=DocumentResponse,
    summary="Attach the signed copy (Path 1)",
    description=(
        "Attach the signed/notarized PDF to the record it was prepared in and "
        "file it in a register book. Only from READY_FOR_SIGNATURE. The signed "
        "copy is queued for processing (`status` QUEUED)."
    ),
)
async def attach_signed_copy(
    document_id: uuid.UUID,
    book_id: uuid.UUID,
    doc_no: int | None = Query(None, ge=1, description="Doc. No. in the register"),
    page_no: int | None = Query(None, ge=1, description="Page No. in the register"),
    file: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(require_lawyer),
    intake: DocumentIntake = Depends(get_intake),
) -> DocumentResponse:
    return await intake.attach_signed_copy(
        current_user, document_id, file, book_id, doc_no, page_no
    )


@router.put(
    "/{document_id}/signed-copy",
    tags=[TAG_SIGNED_COPY],
    response_model=DocumentResponse,
    summary="Replace the signed copy",
    description=(
        "Swap in the right signed copy before finalize. A reason is required and "
        "the previous copy is kept in history. The extraction, chunks and "
        "insights of the previous copy are cleared and the new copy is "
        "processed from the start (`status` QUEUED)."
    ),
)
async def replace_signed_copy(
    document_id: uuid.UUID,
    reason: str = Form(..., min_length=1),
    file: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(require_lawyer),
    intake: DocumentIntake = Depends(get_intake),
) -> DocumentResponse:
    return await intake.replace_signed_copy(current_user, document_id, file, reason)


@router.get(
    "/{document_id}/signed-copies",
    tags=[TAG_SIGNED_COPY],
    response_model=SignedCopyListResponse,
    summary="All signed copies, newest first",
)
async def get_signed_copies(
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_auth),
    queries: DocumentQueries = Depends(get_queries),
) -> SignedCopyListResponse:
    return await queries.get_signed_copies(current_user, document_id)


@router.get(
    "/{document_id}/file",
    tags=[TAG_SIGNED_COPY],
    response_model=SignedCopyFileResponse,
    summary="Short-lived link to a signed copy's PDF",
    description=(
        "Checks the caller's current access, then returns a signed URL valid for "
        "`expires_in` seconds. Defaults to the current copy; pass `copy_id` for "
        "an older one from /signed-copies. Ask again when it expires.\n\n"
        "The owner always gets the signed original. Anyone else gets a "
        "watermarked preview (`watermarked: true`) — for every copy, current or "
        "older — unless their e-copy request was approved for the document's "
        "current file, in which case that file comes unwatermarked."
    ),
    responses={
        200: {"description": "A link to the original, or to a watermarked preview"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        404: {"description": "Document or copy not found, or no access"},
        503: {"description": "The watermarked preview could not be made"},
    },
)
async def get_signed_copy_file(
    document_id: uuid.UUID,
    copy_id: uuid.UUID | None = Query(None),
    current_user: AuthenticatedUser = Depends(require_auth),
    queries: DocumentQueries = Depends(get_queries),
) -> SignedCopyFileResponse:
    return await queries.get_signed_copy_file(current_user, document_id, copy_id)


@router.patch(
    "/{document_id}/entry",
    tags=[TAG_SIGNED_COPY],
    response_model=DocumentResponse,
    summary="Correct the register entry",
    description="Fix the Doc. No. / Page No. (or book) of a SIGNED document before finalize.",
)
async def correct_entry(
    document_id: uuid.UUID,
    body: CorrectEntryRequest,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    intake: DocumentIntake = Depends(get_intake),
) -> DocumentResponse:
    return await intake.correct_entry(
        current_user, document_id, body.doc_no, body.page_no, body.book_id
    )


# ── History ─────────────────────────────────────────────────────────


@router.get(
    "/{document_id}/history",
    tags=[TAG_DOCUMENTS],
    response_model=DocumentHistoryResponse,
    summary="Lifecycle history",
    description="Every lifecycle action on the document, oldest first.",
)
async def get_history(
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_auth),
    queries: DocumentQueries = Depends(get_queries),
) -> DocumentHistoryResponse:
    return await queries.get_history(current_user, document_id)


# ── Invitations: mine ───────────────────────────────────────────────


@router.get(
    "/invitations/mine",
    tags=[TAG_SHARING],
    response_model=DocumentInvitationListResponse,
    status_code=status.HTTP_200_OK,
    summary="List my pending invitations",
    description=(
        "Invitations awaiting this user's response. A pending invitation grants "
        "no access — the document does not appear in `GET /documents/` until it "
        "is accepted. Any authenticated user may call this; it only ever returns "
        "their own invitations."
    ),
    responses={
        200: {"description": "Pending invitations"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
    },
)
async def list_my_invitations(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_auth),
    sharing: DocumentSharing = Depends(get_sharing),
) -> DocumentInvitationListResponse:
    return await sharing.list_invitations(current_user)


# ── Invitations: accept ─────────────────────────────────────────────


@router.post(
    "/invitations/{invitation_id}/accept",
    tags=[TAG_SHARING],
    response_model=DocumentInvitationActionResponse,
    status_code=status.HTTP_200_OK,
    summary="Accept an invitation",
    description=(
        "Grants access to the document. Only the invitee may accept, and only "
        "while the invitation is still pending."
    ),
    responses={
        200: {"description": "Accepted — the document is now accessible"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        404: {"description": "Invitation not found or addressed to someone else"},
        409: {"description": "Already accepted or declined"},
    },
)
async def accept_invitation(
    request: Request,
    invitation_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_auth),
    sharing: DocumentSharing = Depends(get_sharing),
) -> DocumentInvitationActionResponse:
    return await sharing.respond_to_invitation(current_user, invitation_id, accept=True)


# ── Invitations: decline ────────────────────────────────────────────


@router.post(
    "/invitations/{invitation_id}/decline",
    tags=[TAG_SHARING],
    response_model=DocumentInvitationActionResponse,
    status_code=status.HTTP_200_OK,
    summary="Decline an invitation",
    description=(
        "Refuses the invitation; no access is granted. The refusal is recorded "
        "so the owner can see it. The owner may invite again, which reopens it."
    ),
    responses={
        200: {"description": "Declined"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        404: {"description": "Invitation not found or addressed to someone else"},
        409: {"description": "Already accepted or declined"},
    },
)
async def decline_invitation(
    request: Request,
    invitation_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_auth),
    sharing: DocumentSharing = Depends(get_sharing),
) -> DocumentInvitationActionResponse:
    return await sharing.respond_to_invitation(
        current_user, invitation_id, accept=False
    )


# ── Parties: list ───────────────────────────────────────────────────


@router.get(
    "/{document_id}/parties",
    tags=[TAG_SHARING],
    response_model=DocumentPartyListResponse,
    status_code=status.HTTP_200_OK,
    summary="List document parties",
    description="Return the issuer and all whitelisted parties for a document. Accessible by owner, any party, or admin.",
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
    sharing: DocumentSharing = Depends(get_sharing),
) -> DocumentPartyListResponse:
    return await sharing.get_parties(
        current_user,
        document_id,
    )


# ── Parties: add ────────────────────────────────────────────────────


@router.post(
    "/{document_id}/parties",
    tags=[TAG_SHARING],
    response_model=DocumentPartyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a party to a document",
    description="Whitelist a user by email to access this document. Only the document owner (or admin) may add parties. Roles: viewer | signer | editor — signer and editor may only be granted to lawyers.",
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
    sharing: DocumentSharing = Depends(get_sharing),
) -> DocumentPartyResponse:
    party = await sharing.add_party(
        current_user,
        document_id,
        body.email,
        body.role,
    )
    return party_response(party)


# ── Parties: resend an email invitation ─────────────────────────────


@router.post(
    "/{document_id}/parties/{party_id}/resend",
    tags=[TAG_SHARING],
    response_model=DocumentPartyResponse,
    status_code=status.HTTP_200_OK,
    summary="Resend an email invitation",
    description="Email the sign-up link again to someone invited before they had an account, and restart its 7-day expiry. Works on pending and expired email invitations. Only the document owner (or admin) may resend.",
    responses={
        200: {"description": "Invitation resent"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only the document owner may resend"},
        404: {"description": "Document or email invitation not found"},
    },
)
async def resend_party_invite(
    request: Request,
    document_id: uuid.UUID,
    party_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    sharing: DocumentSharing = Depends(get_sharing),
) -> DocumentPartyResponse:
    party = await sharing.resend_invite(current_user, document_id, party_id)
    return party_response(party)


# ── Parties: remove ─────────────────────────────────────────────────


@router.delete(
    "/{document_id}/parties/{party_user_id}",
    tags=[TAG_SHARING],
    response_model=RemovePartyResponse,
    status_code=status.HTTP_200_OK,
    summary="Remove a party from a document",
    description="Revoke a user's access to this document. Only the document owner (or admin) may remove parties.",
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
    sharing: DocumentSharing = Depends(get_sharing),
) -> RemovePartyResponse:
    return await sharing.remove_party(
        current_user,
        document_id,
        party_user_id,
    )


# ── Finalize ────────────────────────────────────────────────────────


@router.post(
    "/{document_id}/finalize",
    tags=[TAG_FINALIZE],
    response_model=RecordResponse,
    status_code=status.HTTP_200_OK,
    summary="Finalize a document",
    description=(
        "The lawyer confirms the signed copy. Freezes it and anchors the SHA-256 "
        "of the PDF on-chain. Only from SIGNED, and only once processing is "
        "COMPLETED (shown as `permissions.can_finalize`), so the risk clauses "
        "were seen first. Nothing is queued. This is the only point at which a "
        "document is hashed on-chain."
    ),
    responses={
        200: {"description": "Finalized document"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only the document owner may finalize"},
        404: {"description": "Document not found"},
        409: {
            "description": (
                "Not SIGNED; processing not COMPLETED (`PROCESSING_NOT_COMPLETE`); "
                "or the stored file no longer matches the upload"
            )
        },
    },
)
async def finalize_document(
    request: Request,
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    finalization: DocumentFinalization = Depends(get_finalization),
) -> RecordResponse:
    return await finalization.finalize(current_user, document_id)


@router.post(
    "/{document_id}/retry-processing",
    tags=[TAG_FINALIZE],
    response_model=RetryProcessingResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry processing a failed document",
    description=(
        "Queue a FAILED document again from the phase that failed: OCR if the "
        "text was never approved, otherwise chunking, embedding and analysis. "
        "Nothing is re-anchored — the on-chain record from finalize stands. "
        "Shown to the client as `permissions.can_retry_processing`."
    ),
    responses={
        202: {"description": "Queued again"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only the document owner may retry"},
        404: {"description": "Document not found"},
        409: {"description": "The document is not FAILED"},
    },
)
async def retry_processing(
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    processing: DocumentProcessing = Depends(get_processing),
) -> RetryProcessingResponse:
    return await processing.retry(current_user, document_id)


# ── Verify ──────────────────────────────────────────────────────────


@router.get(
    "/{document_id}/verify",
    tags=[TAG_FINALIZE],
    response_model=DocumentVerificationResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify a document against the chain",
    description=(
        "Hashes the stored signed copy as it is now and compares it with the "
        "hash anchored on-chain. The verdict is computed server-side. When the "
        "status is TAMPERED, the archived original (re-hashed against the chain) "
        "and the current file are both extracted and diffed: `tamper_report` "
        "lists the changed segments with page and bbox where the OCR engine "
        "reports layout. If that diff cannot be produced, `tamper_report_status` "
        "is UNAVAILABLE and the TAMPERED verdict still stands. A deleted file "
        "is FILE_MISSING, a tamper finding; VERIFICATION_UNAVAILABLE means "
        "storage or the chain could not be read. The chain is read first, so an "
        "anchored document is checked whatever its `lifecycle` says. Every check "
        "that reaches a verdict is kept for the integrity report. With "
        "`report=false` this is the quick check made when a document is opened: "
        "hashes only, no OCR and no `tamper_report`, and an AUTHENTIC or "
        "NOT_ANCHORED verdict is not kept."
    ),
    responses={
        200: {
            "description": (
                "Verification result; `tamper_report` is present only when "
                "status is TAMPERED and `tamper_report_status` is AVAILABLE"
            )
        },
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        404: {
            "description": (
                "Document not found, or a FINALIZED document has no on-chain "
                "record or no stored original"
            )
        },
    },
)
async def verify_document(
    request: Request,
    document_id: uuid.UUID,
    report: bool = Query(
        True, description="Run the OCR diff; false makes this the quick check"
    ),
    current_user: AuthenticatedUser = Depends(require_auth),
    # Untyped: importing BlockchainService here would be circular
    # (blockchain.service imports the documents package).
    verification=Depends(get_verification),
) -> DocumentVerificationResponse:
    return await verification.verify_document(
        current_user, document_id, tamper_report=report, record=True
    )


@router.post(
    "/{document_id}/restore",
    tags=[TAG_FINALIZE],
    response_model=RestoreResponse,
    status_code=status.HTTP_200_OK,
    summary="Repair a tampered document from the chain and its archive",
    description=(
        "Repairs everything found wrong, in one transaction: the lifecycle, the "
        "snapshot row, the file and the metadata. Offer it when the verdict's "
        "`needs_restore` and `permissions.can_restore` are both true. Every value "
        "comes from the chain and the archive, which is found from the on-chain "
        "hash, so an altered row can't block it. The archived original is "
        "re-hashed against the on-chain hash, then "
        "becomes the current signed copy; a tampered file is kept in "
        "/signed-copies and the restore is recorded in /history with both hashes "
        "and, in `changes`, every record value's old and new form. "
        "For a deleted file nothing is preserved, and `preserved_copy_id`, "
        "`previous_hash` and the history row's `from_hash` are null. `repaired` "
        "lists what was put back; `unrepaired` lists what needs a database backup. "
        "The on-chain records are unchanged. If nothing is wrong nothing changes "
        "and `restored` is false, so a retry is safe. "
        "`verification` is a fresh check of the result."
    ),
    responses={
        200: {"description": "Repaired, or nothing was wrong"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only the document owner may restore"},
        404: {"description": "Document not found, or it has no on-chain record"},
        409: {
            "description": (
                "Not FINALIZED, no signed copy record, no archived original, or "
                "the archive does not match the chain"
            )
        },
        503: {"description": "Storage, the chain or the archive is unavailable; nothing changed"},
    },
)
async def restore_document(
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    restoration: DocumentRestoration = Depends(get_restoration),
) -> RestoreResponse:
    return await restoration.restore(current_user, document_id)


# ── E-copy requests ─────────────────────────────────────────────────


@router.post(
    "/{document_id}/ecopy-requests",
    tags=[TAG_ECOPY],
    response_model=EcopyRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Request an e-copy of the signed document",
    description=(
        "A party asks the owner for the unwatermarked signed PDF of a FINALIZED "
        "document. The owner is notified in LexChain. One PENDING request per "
        "client and document; after a decline the client may ask again. Until "
        "approved, GET /{document_id}/file gives the client a watermarked preview."
    ),
    responses={
        201: {"description": "Request created, PENDING"},
        400: {"description": "The caller owns the document"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        404: {"description": "Document not found or no access"},
        409: {
            "description": (
                "Not FINALIZED, a request is already PENDING, or one was already "
                "approved for the current file"
            )
        },
    },
)
async def request_ecopy(
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_auth),
    ecopy: DocumentEcopy = Depends(get_ecopy),
) -> EcopyRequestResponse:
    return await ecopy.request(current_user, document_id)


@router.get(
    "/{document_id}/ecopy-requests",
    tags=[TAG_ECOPY],
    response_model=EcopyRequestListResponse,
    status_code=status.HTTP_200_OK,
    summary="E-copy requests on a document",
    description=(
        "The owner sees every request on the document; a party sees only their "
        "own. Each carries its decision, delivery status and audit trail."
    ),
    responses={
        200: {"description": "Requests, newest first"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        404: {"description": "Document not found or no access"},
    },
)
async def list_document_ecopy_requests(
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_auth),
    ecopy: DocumentEcopy = Depends(get_ecopy),
) -> EcopyRequestListResponse:
    return await ecopy.list_for_document(current_user, document_id)


@router.post(
    "/{document_id}/ecopy-requests/{request_id}/approve",
    tags=[TAG_ECOPY],
    response_model=EcopyRequestResponse,
    status_code=status.HTTP_200_OK,
    summary="Approve an e-copy request and email the copy",
    description=(
        "Approves the document's current signed copy for this client, then "
        "emails it as an attachment to the client's verified account address. "
        "The approval stands whether or not the email goes out: check "
        "`delivery_status` — FAILED (with `delivery_error`) can be retried with "
        "/retry-delivery. Once approved the client can download the "
        "unwatermarked file in the app while they keep access and the file is "
        "unchanged."
    ),
    responses={
        200: {"description": "Approved; delivery SENT or FAILED"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only the document owner decides"},
        404: {"description": "Document or request not found"},
        409: {
            "description": (
                "Already decided, the client no longer has access, or the "
                "document has no finalized signed copy"
            )
        },
    },
)
async def approve_ecopy_request(
    document_id: uuid.UUID,
    request_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    ecopy: DocumentEcopy = Depends(get_ecopy),
) -> EcopyRequestResponse:
    return await ecopy.approve(current_user, document_id, request_id)


@router.post(
    "/{document_id}/ecopy-requests/{request_id}/decline",
    tags=[TAG_ECOPY],
    response_model=EcopyRequestResponse,
    status_code=status.HTTP_200_OK,
    summary="Decline an e-copy request",
    description=(
        "Declines the request, optionally with a reason shown to the client, "
        "who is notified and may request again."
    ),
    responses={
        200: {"description": "Declined"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only the document owner decides"},
        404: {"description": "Document or request not found"},
        409: {"description": "Already decided"},
    },
)
async def decline_ecopy_request(
    document_id: uuid.UUID,
    request_id: uuid.UUID,
    body: DeclineEcopyRequest,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    ecopy: DocumentEcopy = Depends(get_ecopy),
) -> EcopyRequestResponse:
    return await ecopy.decline(current_user, document_id, request_id, body.reason)


@router.post(
    "/{document_id}/ecopy-requests/{request_id}/retry-delivery",
    tags=[TAG_ECOPY],
    response_model=EcopyRequestResponse,
    status_code=status.HTTP_200_OK,
    summary="Send an approved e-copy again",
    description=(
        "For an APPROVED request whose `delivery_status` is FAILED, or stuck in "
        "SENDING for over 10 minutes. Access, the verified address and the "
        "file's fingerprint are checked again before sending; only one retry "
        "runs at a time."
    ),
    responses={
        200: {"description": "Attempted; delivery SENT or FAILED"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only the document owner may retry"},
        404: {"description": "Document or request not found"},
        409: {"description": "Not approved, already delivered, or a send is in progress"},
    },
)
async def retry_ecopy_delivery(
    document_id: uuid.UUID,
    request_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    ecopy: DocumentEcopy = Depends(get_ecopy),
) -> EcopyRequestResponse:
    return await ecopy.retry_delivery(current_user, document_id, request_id)


# ── OCR review (extraction/router.py) ───────────────────────────────

router.include_router(extraction_router)
