import uuid
from datetime import datetime

from pydantic import BaseModel, Field, ConfigDict


class DocumentSnapshotResponse(BaseModel):
    document_id: uuid.UUID = Field(
        ..., description="document id of the the document in the snapshot"
    )
    signed_copy_id: uuid.UUID = Field(..., description="The signed copy that was frozen")
    file_sha256: str = Field(
        ..., description="SHA-256 of the final PDF bytes — the hash anchored on-chain"
    )
    size_bytes: int
    created_at: datetime = Field(
        ..., description="When the document snapshot was created"
    )
    updated_at: datetime | None = Field(
        None, description="When the document snapshot was updated"
    )

    model_config = ConfigDict(from_attributes=True)
