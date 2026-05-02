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


class DocumentUploadAcceptedResponse(BaseModel):
    """Response payload returned immediately after upload is accepted."""

    document_id: uuid.UUID = Field(..., description="Document identifier.")
    status: str = Field(..., description="Current document status after upload.")
    message: str = Field(..., description="Human-readable processing state.")
