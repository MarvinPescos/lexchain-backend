from pydantic import BaseModel, Field
import uuid
from datetime import datetime

class PublicVerifyResponse(BaseModel):
    status: str                          # "verified" | "possible_match" | "not_found"
    confidence: float                    # 100.0 = exact match, <100.0 = fuzzy percentage
    file_name: str | None = None
    storage_url: str | None = None
    recorded_at: int | None = None       # unix timestamp from chain
    recorded_by: str | None = None       # wallet address
    tx_hash: str | None = None
    matched_at: datetime

class PublicVerifyMatch(BaseModel):
    """Lightweight info about a matched document."""
    file_name: str
    status: str
    on_chain: bool
    recorded_at: int | None
    tx_hash: str | None
    matched_at: datetime