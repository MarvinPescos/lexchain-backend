from pydantic import BaseModel, ConfigDict, Field
import uuid
from datetime import datetime
from typing import Literal


class RecordResponse(BaseModel):
    """Response returned after recording a document on-chain."""

    document_id: uuid.UUID = Field(..., description="Document identifier.")
    tx_hash: str = Field(..., description="Transaction hash on the blockchain")
    onchain_document_id: str = Field(..., description="Document Id stored on-chain")
    data_hash: str = Field(..., description="SHA256 hash stored on-chain.")


class WordDiffPart(BaseModel):
    """One run of words inside a changed segment, for inline highlighting."""

    op: Literal["equal", "removed", "added"]
    text: str


class TamperSegment(BaseModel):
    """One changed region between the finalized original and the current file."""

    type: Literal["replace", "insert", "delete"]
    severity: Literal["critical", "major", "minor"] = Field(
        ..., description="critical: amounts or reference numbers; major: dates or figures"
    )
    reason: str = Field(..., description="What changed, in plain terms")
    original_text: str = Field(
        ..., description="Text in the finalized original; empty for an insert"
    )
    current_text: str = Field(..., description="Text in the current file; empty for a delete")
    original_line_start: int = Field(..., description="1-based block position in the original")
    original_line_end: int
    current_line_start: int = Field(..., description="1-based block position in the current file")
    current_line_end: int
    block_index: int | None = Field(
        None,
        description=(
            "Block in the current file's extraction; for a delete, the block in "
            "the original's extraction"
        ),
    )
    original_block_index: int | None = Field(
        None, description="Block in the original's extraction the change starts at"
    )
    location: Literal["current", "original"] = Field(
        ...,
        description=(
            "Which PDF `page_idx`/`bbox` refer to: the current file, or — for a "
            "delete, whose text is no longer there — the finalized original"
        ),
    )
    page_idx: int | None = Field(
        None, description="Zero-based page; null when the OCR engine reports no layout"
    )
    bbox: list[float] | None = Field(
        None,
        description=(
            "[x0, y0, x1, y1] normalized to a 0-1000 box per page, same convention "
            "as ExtractionBlock.bbox; null when the OCR engine reports no layout"
        ),
    )
    word_diff: list[WordDiffPart]


class TamperReport(BaseModel):
    """Where the current file differs from the finalized original.

    The baseline is the archived original, re-hashed against the chain before
    use; both sides are extracted fresh from their bytes by the same engine.
    Segments are ordered most severe first.
    """

    localized: bool = Field(
        ...,
        description=(
            "Whether the OCR engine reported layout; when false every segment's "
            "`page_idx` and `bbox` are null and the diff is text-only"
        ),
    )
    total_changes: int = Field(..., description="Number of segments returned")
    critical_changes: int
    replaced: int = Field(..., description="Changed regions found, including any not returned")
    inserted: int
    deleted: int
    similarity: float = Field(..., description="0-1 text similarity, original vs current")
    truncated: bool = Field(
        ..., description="Whether more changes were found than `segments` returns"
    )
    segments: list[TamperSegment]


class DocumentVerificationResponse(BaseModel):
    """Result of verifying a finalized document against its on-chain anchor.

    `status` is decided server-side by recomputing the hash — never by the
    client — and `baseline_trusted` says whether the stored original could
    itself be proven genuine before being used as the diff baseline.
    """

    document_id: uuid.UUID
    file_name: str | None = Field(None, description="Display name of the document")
    status: str = Field(
        ...,
        description=(
            "AUTHENTIC — the stored file matches the chain; "
            "TAMPERED — it does not; "
            "FILE_MISSING — the stored file was deleted (a tamper finding; "
            "`current_hash` is null and there is no diff); "
            "SNAPSHOT_COMPROMISED — the file was changed or deleted and the stored "
            "original was altered too, so no trustworthy baseline exists to diff "
            "against; "
            "NOT_ANCHORED — the document was never finalized and the chain has no "
            "record of it; "
            "VERIFICATION_UNAVAILABLE — storage or the chain could not be read, or "
            "an anchor exists with no finalized state or frozen record to check it "
            "against, so integrity is unknown (not a tamper finding by itself)"
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

    tamper_report_status: Literal["AVAILABLE", "UNAVAILABLE", "NOT_APPLICABLE"] = Field(
        "NOT_APPLICABLE",
        description=(
            "AVAILABLE — `tamper_report` holds a diff against a trusted baseline; "
            "UNAVAILABLE — the document is TAMPERED but no trustworthy diff could "
            "be produced (the verdict still stands); NOT_APPLICABLE — any status "
            "other than TAMPERED, or the quick check, which makes no diff"
        ),
    )
    tamper_report: TamperReport | None = Field(
        None, description="Present only when `tamper_report_status` is AVAILABLE"
    )
    tamper_report_unavailable_reason: str | None = Field(
        None, description="Why no diff, when `tamper_report_status` is UNAVAILABLE"
    )


class VerificationResultResponse(BaseModel):
    """One stored check, as it was decided at `verified_at`."""

    id: uuid.UUID
    document_id: uuid.UUID
    file_name: str | None = Field(None, description="Display name of the document now")
    verified_at: datetime = Field(..., description="When the check ran (UTC)")
    verified_by: uuid.UUID | None = Field(None, description="Who ran the check")
    status: str = Field(
        ...,
        description=(
            "AUTHENTIC | TAMPERED | FILE_MISSING | SNAPSHOT_COMPROMISED | "
            "NOT_ANCHORED | VERIFICATION_UNAVAILABLE — as on DocumentVerificationResponse. "
            "VERIFICATION_UNAVAILABLE means integrity was unknown, not tampered"
        ),
    )
    is_authentic: bool
    baseline_trusted: bool = Field(
        ..., description="Whether the stored original matched the chain at the time"
    )
    onchain_hash: str | None = None
    snapshot_hash: str | None = None
    current_hash: str | None = None
    tx_hash: str | None = None
    tamper_report_status: Literal["AVAILABLE", "UNAVAILABLE", "NOT_APPLICABLE"]

    model_config = ConfigDict(from_attributes=True)


class IntegrityReportSummary(BaseModel):
    """Counts over every result in the range, not just the returned page."""

    total: int
    authentic: int = 0
    tampered: int = 0
    file_missing: int = 0
    snapshot_compromised: int = 0
    not_anchored: int = 0
    verification_unavailable: int = 0


class IntegrityReportResponse(BaseModel):
    verified_from: datetime | None = Field(
        None, description="Inclusive lower bound on verified_at (UTC); null for no bound"
    )
    verified_to: datetime | None = Field(
        None, description="Exclusive upper bound on verified_at (UTC); null for no bound"
    )
    limit: int
    offset: int
    summary: IntegrityReportSummary
    results: list[VerificationResultResponse] = Field(
        default_factory=list,
        description="One page, newest first; ties on verified_at ordered by id",
    )


ChainReconcileStatus = Literal[
    "ok", "snapshot_missing", "orphaned_anchor", "document_missing"
]


class ChainReconcileCheck(BaseModel):
    document_id: str = Field(
        ..., description="The ID as anchored; no database row may exist for it"
    )
    status: ChainReconcileStatus = Field(
        ...,
        description=(
            "snapshot_missing: the document is FINALIZED but its snapshot row is "
            "gone. orphaned_anchor: the document isn't FINALIZED, so finalize "
            "failed after anchoring or the lifecycle was edited; not reported as "
            "tampering. document_missing: the document row is gone."
        ),
    )


class ChainReconcileResponse(BaseModel):
    skipped: bool = Field(
        default=False,
        description="True when CHAIN_SCAN_FROM_BLOCK isn't set: nothing was checked",
    )
    checked: int = Field(default=0, description="Document IDs anchored on-chain")
    ok: int = 0
    snapshot_missing: int = 0
    orphaned_anchor: int = 0
    document_missing: int = 0
    problems: list[ChainReconcileCheck] = Field(
        default_factory=list, description="Every anchored ID whose status is not ok"
    )
