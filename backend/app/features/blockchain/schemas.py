from pydantic import BaseModel, Field
import uuid
from datetime import datetime


class RecordResponse(BaseModel):
    """Response returned after recording a document on-chain."""

    document_id: uuid.UUID = Field(..., description="Document identifier.")
    tx_hash: str = Field(..., description="Transaction hash on the blockchain")
    onchain_document_id: str = Field(..., description="Document Id stored on-chain")
    data_hash: str = Field(..., description="SHA256 hash stored on-chain.")
    
class OnChainVerificationResponse(BaseModel):
    """Response returned when verifying an on-chain record."""

    document_id: uuid.UUID = Field(..., description="Document identifier.")
    onchain_document_id: str = Field(..., description="Document ID on-chain.")
    data_hash: str = Field(..., description="Hash stored on-chain.")
    onchain_timestamp: int = Field(..., description="Unix timestamp when strored.")
    issued_by: str = Field(..., description="Address that issued the transaction.")
    verified_at: datetime = Field(..., description="When this verification was performed.")


class DocumentVerificationResponse(BaseModel):
    """Result of verifying a finalized document against its on-chain anchor.

    `status` is decided server-side by recomputing the hash — never by the
    client — and `baseline_trusted` says whether the stored original could
    itself be proven genuine before being used as the diff baseline.
    """

    document_id: uuid.UUID
    file_name: str | None = Field(None, description="Display name of the document")
    storage_url: str | None = Field(
        None, description="URL of the document as it stands now, for rendering pages"
    )
    status: str = Field(
        ...,
        description=(
            "AUTHENTIC — the stored file matches the chain; "
            "TAMPERED — it does not; "
            "SNAPSHOT_COMPROMISED — the stored original was altered too, so no "
            "trustworthy baseline exists to diff against; "
            "NOT_ANCHORED — the document was never finalized; "
            "VERIFICATION_UNAVAILABLE — the document could not be re-read, so "
            "integrity is unknown (this is not a tamper result)"
        ),
    )
    is_authentic: bool
    baseline_trusted: bool = Field(
        ..., description="Whether the stored original still matches the chain"
    )

    onchain_hash: str | None = Field(None, description="Hash read back from the chain")
    snapshot_hash: str | None = Field(None, description="Hash of the stored original")
    current_hash: str | None = Field(None, description="Hash of the document as it is now")

    tx_hash: str | None = None
    onchain_timestamp: int | None = None
    issued_by: str | None = None
    finalized_at: datetime | None = None
    verified_at: datetime

    message: str = Field(..., description="Human-readable verdict")
