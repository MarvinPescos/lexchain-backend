import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ExtractionBlock(BaseModel):
    """One reviewable piece of the document."""

    index: int = Field(
        ..., description="Stable id and reading order; use it when sending edits"
    )
    editable: bool = Field(
        True,
        description=(
            "False for figures, seals and signatures — outline them on the page "
            "but they cannot be edited and are excluded from the approved text"
        ),
    )
    type: str = Field("text", description="Block type reported by the OCR engine")
    text: str = Field(..., description="Current text — the issuer's edit if edited")
    original_text: str = Field(..., description="What the OCR engine produced")
    bbox: list[float] | None = Field(
        None,
        description=(
            "[x0, y0, x1, y1] normalized to a 0-1000 box per page. Both axes are "
            "scaled independently, so page aspect ratio is NOT preserved: map with "
            "x/1000*renderedWidth and y/1000*renderedHeight separately. Null for "
            "engines that report no layout."
        ),
    )
    page_idx: int | None = Field(None, description="Zero-based page the block sits on")
    text_level: int | None = Field(None, description="Heading level, if a heading")
    score: float | None = Field(None, description="OCR confidence for this block")
    is_html: bool = Field(
        False, description="Content is HTML (tables) rather than plain text"
    )
    edited: bool = Field(False, description="Whether the issuer changed this block")


class ExtractionFlag(BaseModel):
    """Something the issuer should look at."""

    block_index: int | None = Field(
        None, description="Block the flag points at; null if document-wide"
    )
    kind: str = Field(..., description="e.g. low_confidence, missing_space, amount_mismatch")
    severity: str = Field(..., description="high | medium | low")
    message: str = Field(..., description="What looks wrong, in plain terms")
    excerpt: str = Field("", description="The offending text")


class ExtractionReviewResponse(BaseModel):
    """Everything the review screen needs."""

    document_id: uuid.UUID
    extraction_id: uuid.UUID
    status: str = Field(..., description="Document processing status")
    file_name: str = Field(..., description="Display name of the document")
    signed_copy_id: uuid.UUID = Field(
        ...,
        description=(
            "The PDF behind the blocks; read it through "
            "GET /documents/{document_id}/file for rendering pages"
        ),
    )
    engine: str
    page_count: int
    confidence_avg: float | None = None
    blocks: list[ExtractionBlock]
    flags: list[ExtractionFlag]
    flag_count: int
    high_severity_count: int
    edited_block_count: int
    is_reviewed: bool
    reviewed_by: uuid.UUID | None = None
    reviewed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class BlockEdit(BaseModel):
    """A correction to one block."""

    index: int = Field(..., ge=0, description="Index of the block being corrected")
    text: str = Field(..., description="Corrected text for the block")


class UpdateExtractionRequest(BaseModel):
    """Save corrections without approving yet — callable repeatedly."""

    edits: list[BlockEdit] = Field(..., min_length=1, description="Blocks to correct")


class ApproveExtractionResponse(BaseModel):
    """Result of approving the extracted text."""

    document_id: uuid.UUID
    status: str
    edited_block_count: int
    content_hash: str = Field(..., description="Hash of the approved text")
    message: str
