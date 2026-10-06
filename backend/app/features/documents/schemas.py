import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


LIFECYCLE_DESCRIPTION = "PREPARING | READY_FOR_SIGNATURE | SIGNED | FINALIZED | CANCELLED"


class DocumentUploadResponse(BaseModel):
    """A document in the list view."""

    id: uuid.UUID = Field(..., description="Unique identifier of the document.")
    file_name: str = Field(..., description="Display name")
    lifecycle: str = Field(..., description=LIFECYCLE_DESCRIPTION)
    status: str | None = Field(
        None, description="Processing status; null until the document is finalized"
    )
    on_chain: bool = Field(..., description="Check if doc is on chain")
    is_owner: bool = Field(
        True, description="True if the user owns this document, false if invited to it"
    )
    my_role: str = Field(
        "owner",
        description="This user's role on the document: owner | viewer | signer | editor",
    )
    shared_at: datetime | None = Field(
        None, description="When the user was invited; null for documents they own"
    )
    book_id: uuid.UUID | None = Field(None, description="Register book it is filed in")
    doc_no: int | None = Field(None, description="Doc. No. in the register")
    page_no: int | None = Field(None, description="Page No. in the register")
    created_at: datetime = Field(..., description="When the document was created")

    class Config:
        from_attributes = True


class SignedCopyResponse(BaseModel):
    """One uploaded signed/notarized PDF."""

    id: uuid.UUID
    storage_url: str
    sha256: str = Field(..., description="SHA-256 of the PDF bytes")
    content_type: str
    size_bytes: int
    original_filename: str | None = None
    uploaded_by: uuid.UUID | None = None
    replaced_reason: str | None = Field(
        None, description="Why this copy replaced the previous one"
    )
    is_current: bool = Field(
        False, description="Whether this is the copy finalize will freeze"
    )
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DocumentPermissions(BaseModel):
    """What the requesting user is allowed to do with this document.

    Mirrors the server-side checks so the client can hide actions it would be
    rejected for — the API still enforces them independently.
    """

    can_view: bool = Field(True, description="Always true if the document was returned")
    can_rename: bool = Field(..., description="May change the display name")
    can_create_draft: bool = Field(..., description="May create the Google Docs draft")
    can_mark_ready: bool = Field(..., description="May mark it ready for signature")
    can_reopen: bool = Field(..., description="May send it back to preparing")
    can_attach_signed_copy: bool = Field(..., description="May attach the signed PDF")
    can_replace_signed_copy: bool = Field(..., description="May replace the signed PDF")
    can_correct_entry: bool = Field(..., description="May correct Doc. No. / Page No.")
    can_cancel: bool = Field(..., description="May cancel the document")
    can_finalize: bool = Field(..., description="May snapshot and anchor on-chain")
    can_share: bool = Field(..., description="May whitelist other users")
    can_revoke: bool = Field(..., description="May remove whitelisted users")


class DocumentResponse(BaseModel):
    """Document detail response with analysis insight."""

    document_id: uuid.UUID = Field(..., description="Document identifier.")
    file_name: str = Field(..., description="Display name")
    lifecycle: str = Field(..., description=LIFECYCLE_DESCRIPTION)
    status: str | None = Field(
        None, description="Processing status; null until the document is finalized"
    )
    on_chain: bool = Field(
        ..., description="Check if document is already recorded on-chain or not"
    )
    draft_url: str | None = Field(
        None,
        description="Working draft in Google Docs. Reference only — not the legal document",
    )
    signed_copy: SignedCopyResponse | None = Field(
        None, description="The current signed copy — what finalize will freeze"
    )
    document_hash: str | None = Field(
        None,
        description=(
            "SHA-256 of the current signed copy. This is the value anchored "
            "on-chain at finalize, so it is what verification compares against."
        ),
    )
    # Where the document sits in the register; null until the signed copy is filed.
    book_id: uuid.UUID | None = Field(None, description="Register book it is filed in")
    doc_no: int | None = Field(None, description="Doc. No. in the register")
    page_no: int | None = Field(None, description="Page No. in the register")
    finalized_at: datetime | None = Field(
        None, description="When the document was finalized and anchored"
    )
    finalized_by: uuid.UUID | None = Field(
        None, description="User who finalized the document"
    )
    cancelled_at: datetime | None = None
    permissions: "DocumentPermissions" = Field(
        ..., description="What the requesting user may do with this document"
    )
    summary: str | None = Field(None, description="LLM-generated summary")
    labels: list[str] = Field(default_factory=list, description="Document type labels")
    entities: list[dict] = Field(
        default_factory=list, description="Named entities extracted"
    )
    risk_flags: list[dict] = Field(
        default_factory=list, description="Identified risk flags"
    )
    created_at: datetime
    updated_at: datetime | None = None


class OpenDocumentRequest(BaseModel):
    """Path 1 — open a document record before anything is signed."""

    file_name: str = Field(..., min_length=1, max_length=255)


class UpdateDocumentRequest(BaseModel):
    """Rename the document."""

    file_name: str = Field(..., min_length=1, max_length=255)


class CreateDraftRequest(BaseModel):
    """Create the working draft in the lawyer's Google Drive."""

    source: Literal["blank", "template", "existing"] = Field(
        "blank",
        description=(
            "blank — a new empty Doc; template — a copy of `file_id`; "
            "existing — `file_id` itself, a Doc the lawyer already started"
        ),
    )
    file_id: str | None = Field(
        None,
        description="Drive file id from the Google Picker. Required unless source is blank",
    )


class CorrectEntryRequest(BaseModel):
    """Correct the register entry of a signed document."""

    book_id: uuid.UUID | None = Field(None, description="Defaults to the current book")
    doc_no: int = Field(..., ge=1)
    page_no: int = Field(..., ge=1)


class StageChangeResponse(BaseModel):
    """One lifecycle action on a document."""

    from_stage: str | None
    to_stage: str
    action: str
    actor_id: uuid.UUID | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DocumentHistoryResponse(BaseModel):
    document_id: uuid.UUID
    changes: list[StageChangeResponse]


class SignedCopyListResponse(BaseModel):
    document_id: uuid.UUID
    copies: list[SignedCopyResponse]


# Add Parties


class AddPartyRequest(BaseModel):
    """Request to whitelist a user on a document"""

    email: str = Field(..., description="Email of the user to whitelist.")
    role: str = Field(default="viewer", description="Role: viewer | signer | editor")


class DocumentInvitationResponse(BaseModel):
    """An invitation to a document, awaiting the invitee's response."""

    invitation_id: uuid.UUID = Field(..., description="Use this to accept or decline")
    document_id: uuid.UUID
    file_name: str = Field(..., description="Name of the document you were invited to")
    role: str = Field(..., description="Role you would hold: viewer | signer | editor")
    status: str = Field(..., description="pending | accepted | declined")
    invited_at: datetime = Field(..., description="When the invitation was sent")
    responded_at: datetime | None = Field(
        None, description="When you accepted or declined; null while pending"
    )

    class Config:
        from_attributes = True


class DocumentInvitationListResponse(BaseModel):
    """Invitations awaiting this user's response."""

    invitations: list[DocumentInvitationResponse] = Field(default_factory=list)
    total: int


class DocumentInvitationActionResponse(BaseModel):
    """Result of accepting or declining."""

    invitation_id: uuid.UUID
    document_id: uuid.UUID
    status: str
    message: str


class DocumentPartyResponse(BaseModel):
    """A single party record on a document"""

    id: uuid.UUID = Field(..., description="Party record ID.")
    user_id: uuid.UUID = Field(..., description="User ID")
    email: str = Field(..., description="User's email")
    f_name: str = Field(..., description="User's first Name")
    l_name: str = Field(..., description="User's last name")
    role: str = Field(..., description="Role assigned")
    status: str = Field(
        "accepted", description="pending | accepted | declined — the owner is always accepted"
    )
    responded_at: datetime | None = Field(
        None, description="When this party accepted or declined"
    )
    created_at: datetime

    class Config:
        from_attributes = True


class DocumentPartyListResponse(BaseModel):
    """List of parties for a document"""

    document_id: uuid.UUID = Field(..., description="Document identifier.")
    issuer: DocumentPartyResponse = Field(
        ..., description="The owner/issuer of the document"
    )
    parties: list[DocumentPartyResponse] = Field(default_factory=list)

# class DocumentFinalizedResponse(BaseModel):
    # """show"""


class RemovePartyResponse(BaseModel):
    """Confirmation after removing a party"""

    document_id: uuid.UUID
    user_id: uuid.UUID
    message: str


# Draft comments (mirrored from Google Docs)


class DraftReplyResponse(BaseModel):
    author_name: str
    author_user_id: uuid.UUID | None = None
    is_lawyer: bool = False
    content: str
    action: str | None = Field(None, description="resolve | reopen, when the reply did that")
    created_at: datetime


class DraftCommentResponse(BaseModel):
    id: uuid.UUID
    author_name: str
    author_user_id: uuid.UUID | None = Field(
        None, description="The LexChain user, when the Google author could be matched"
    )
    is_lawyer: bool = False
    content: str
    quoted_text: str | None = Field(None, description="The draft text the comment is on")
    resolved: bool
    created_at: datetime
    replies: list[DraftReplyResponse] = Field(default_factory=list)

    @classmethod
    def from_comment(cls, comment, document) -> "DraftCommentResponse":
        return cls(
            id=comment.id,
            author_name=comment.author_name,
            author_user_id=comment.author_user_id,
            is_lawyer=comment.author_user_id == document.user_id,
            content=comment.content,
            quoted_text=comment.quoted_text,
            resolved=comment.resolved,
            created_at=comment.remote_created_at,
            replies=[
                DraftReplyResponse(
                    author_name=r.author_name,
                    author_user_id=r.author_user_id,
                    is_lawyer=r.author_user_id == document.user_id,
                    content=r.content,
                    action=r.action,
                    created_at=r.remote_created_at,
                )
                for r in comment.replies
                if not r.deleted
            ],
        )


class DraftCommentListResponse(BaseModel):
    document_id: uuid.UUID
    synced_at: datetime | None = Field(None, description="Last sync from Google Docs")
    unresolved: int = Field(..., description="Open threads; Ready is refused while > 0")
    comments: list[DraftCommentResponse]
