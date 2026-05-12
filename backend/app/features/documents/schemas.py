from pydantic import BaseModel, Field
import uuid
from datetime import datetime


class DocumentUploadResponse(BaseModel):
    """
    
    """

    id: uuid.UUID = Field(..., description="Unique identifier of the document.")
    file_name: str = Field(..., description="Name of the uploaded file")
    content_type: str = Field(..., description="MIME type of the uploaded file")
    status: str = Field(..., description="Processing status of the document")
    created_at: datetime = Field(..., description="When the document was uploaded")

    class Config:
        from_attributes = True

class DocumentResponse(BaseModel):
    """Document detail response with analysis insight."""

    document_id: uuid.UUID = Field(..., description="Document identifier.")
    file_name: str = Field(..., description="Name of the uploaded file")
    content_type: str = Field(..., description="MIME type of the uploaded file")
    status: str = Field(..., description="Processing status of the document")
    summary: str | None = Field(None, description="LLM-generated summary")
    labels: list[str] = Field(default_factory=list, description="Document type labels")
    entities: list[dict] = Field(default_factory=list, description="Named entities extracted")
    risk_flags: list[dict] = Field(default_factory=list, description="Identified risk flags")
    created_at: datetime
    updated_at: datetime



class DocumentUploadAcceptedResponse(BaseModel):
    """Response payload returned immediately after upload is accepted."""

    document_id: uuid.UUID = Field(..., description="Document identifier.")
    status: str = Field(..., description="Current document status after upload.")
    message: str = Field(..., description="Human-readable processing state.")


class RenameDocumentRequest(BaseModel):
    """Request payload for renaming a document."""

    file_name: str = Field(..., min_length=1, max_length=255, description="New display name for the document.")


# Add Parties

class AddPartyRequest(BaseModel):
    """Request to whitelist a user on a document"""

    email: str = Field(..., description="Email of the user to whitelist.")
    role: str = Field(default="viewer", description="Role: viewer | signer | editor")

class DocumentPartyResponse(BaseModel):
    """A single party record on a document"""
    
    id: uuid.UUID = Field(...,description="Party record ID.")
    user_id: uuid.UUID = Field(..., description="User ID")
    role: str = Field(..., description="Role assigned")
    created_at: datetime

    class Config:
        from_attributes=True

class DocumentPartyListResponse(BaseModel):
    """List of parties for a document"""
    document_id: uuid.UUID = Field(..., description="Document identifier.")
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

