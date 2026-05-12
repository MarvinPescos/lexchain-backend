from pydantic import BaseModel, Field
import uuid
from datetime import datetime

class NotarizeResponse(BaseModel):
    """Response returned after notarizing a document on-chain."""
    
    document_id: uuid.UUID = Field(..., description="Document identifier.")
    tx_hash: str = Field(..., description="Transaction hash on the blockchain")
    onchain_document_id: str = Field(..., description="Document Id stored on-chain")
    data_hash: str =Field(..., description="SHA256 hash stored on-chain.")
    
class OnChainVerificationResponse(BaseModel):
    """Response returned when verifying an on-chain record."""

    document_id: uuid.UUID = Field(..., description="Document identifier.")
    onchain_document_id: str = Field(..., description="Document ID on-chain.")
    data_hash: str = Field(..., description="Hash stored on-chain.")
    onchain_timestamp: int = Field(..., description="Unix timestamp when strored.")
    issued_by: str = Field(..., description="Address that issued the transaction.")
    verified_at: datetime = Field(..., description="When this verification was performed.")
