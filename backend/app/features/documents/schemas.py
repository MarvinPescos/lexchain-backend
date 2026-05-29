import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class DocumentUploadResponse(BaseModel):
    """ """

    id: uuid.UUID = Field(..., description="Unique identifier of the document.")
    file_name: str = Field(..., description="Name of the uploaded file")
    content_type: str = Field(..., description="MIME type of the uploaded file")
    status: str = Field(..., description="Processing status of the document")
    on_chain: bool = Field(..., description="Check if doc is on chain")
    created_at: datetime = Field(..., description="When the document was uploaded")

    class Config:
        from_attributes = True


class DocumentResponse(BaseModel):
    """Document detail response with analysis insight."""

    document_id: uuid.UUID = Field(..., description="Document identifier.")
    file_name: str = Field(..., description="Name of the uploaded file")
    storage_url: str = Field(..., description="Storage Url of the Document")
    content_type: str = Field(..., description="MIME type of the uploaded file")
    status: str = Field(..., description="Processing status of the document")
    on_chain: bool = Field(
        ..., description="Check if document is already recorded on-chain or not"
    )
    is_latest: bool = Field(..., description="Check if document is the latest one")
    summary: str | None = Field(None, description="LLM-generated summary")
    labels: list[str] = Field(default_factory=list, description="Document type labels")
    entities: list[dict] = Field(
        default_factory=list, description="Named entities extracted"
    )
    risk_flags: list[dict] = Field(
        default_factory=list, description="Identified risk flags"
    )
    created_at: datetime
    updated_at: datetime


class DocumentUploadAcceptedResponse(BaseModel):
    """Response payload returned immediately after upload is accepted."""

    document_id: uuid.UUID = Field(..., description="Document identifier.")
    status: str = Field(..., description="Current document status after upload.")
    message: str = Field(..., description="Human-readable processing state.")


class RenameDocumentRequest(BaseModel):
    """Request payload for renaming a document."""

    file_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="New display name for the document.",
    )


# Add Parties


class AddPartyRequest(BaseModel):
    """Request to whitelist a user on a document"""

    email: str = Field(..., description="Email of the user to whitelist.")
    role: str = Field(default="viewer", description="Role: viewer | signer | editor")


class DocumentPartyResponse(BaseModel):
    """A single party record on a document"""

    id: uuid.UUID = Field(..., description="Party record ID.")
    user_id: uuid.UUID = Field(..., description="User ID")
    email: str = Field(..., description="User's email")
    f_name: str = Field(..., description="User's first Name")
    l_name: str = Field(..., description="User's last name")
    role: str = Field(..., description="Role assigned")
    # status: str = Field(..., description="")
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


class RemovePartyResponse(BaseModel):
    """Confirmation after removing a party"""

    document_id: uuid.UUID
    user_id: uuid.UUID
    message: str


# Version history


class VersionHistoryItem(BaseModel):
    """A single entry ion the document's version chain"""

    document_id: uuid.UUID
    file_name: str
    document_hash: str
    status: str
    tx_hash: str | None
    is_latest: bool
    created_at: datetime


class VersionHistoryResponse(BaseModel):
    """The full version chain for a document"""

    current_document_id: uuid.UUID
    versions: list[VersionHistoryItem]
    total_version: int
