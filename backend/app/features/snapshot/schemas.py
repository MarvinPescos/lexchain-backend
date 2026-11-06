import uuid
from datetime import datetime
from typing import Literal

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
    backed_up_at: datetime | None = Field(
        None,
        description=(
            "When the finalized bytes were archived and verified against the "
            "anchored hash. Null means no recoverable archive."
        ),
    )
    created_at: datetime = Field(
        ..., description="When the document snapshot was created"
    )
    updated_at: datetime | None = Field(
        None, description="When the document snapshot was updated"
    )

    model_config = ConfigDict(from_attributes=True)


BackupStatus = Literal[
    "ok", "missing", "mismatch", "unverified", "unavailable", "repaired", "unrecoverable"
]

MetadataStatus = Literal["MATCH", "ALTERED", "NOT_ANCHORED", "UNAVAILABLE"]


class RecordChange(BaseModel):
    """One database value that differs from what the chain and archive support."""

    field: str
    anchored: str | int | None = Field(
        None, description="The genuine value; null if nothing proves one"
    )
    current: str | int | None = Field(None, description="The value in the database now")


class BackupCheck(BaseModel):
    document_id: uuid.UUID
    status: BackupStatus = Field(
        ...,
        description=(
            "ok: archive present and matches the anchored hash. missing: no "
            "archive. mismatch: the archive's bytes, the snapshot and the "
            "anchored hash do not all agree. unverified: the chain had no record "
            "or could not be read, so integrity is unknown. unavailable: the archive "
            "could not be read, so its state is unknown. repaired: re-archived "
            "from the live copy. unrecoverable: no good archive and the live copy "
            "no longer matches either."
        ),
    )
    backup_key: str | None = None


class LiveFileCheck(BaseModel):
    document_id: uuid.UUID
    status: str = Field(
        ...,
        description=(
            "TAMPERED: the live file differs from the anchored hash. FILE_MISSING: "
            "the live file was deleted. SNAPSHOT_COMPROMISED: either of those, and "
            "the snapshot row differs from the chain too. VERIFICATION_UNAVAILABLE: "
            "storage or the chain could not be read, so integrity is unknown."
        ),
    )


class LiveFileReport(BaseModel):
    checked: int
    ok: int
    tampered: int
    file_missing: int
    snapshot_compromised: int
    unavailable: int
    problems: list[LiveFileCheck] = Field(
        default_factory=list, description="Every live file that is not ok"
    )


RecordKind = Literal["metadata", "snapshot", "lifecycle"]


class RecordFinding(RecordChange):
    document_id: uuid.UUID
    kind: RecordKind


class RecordReport(BaseModel):
    checked: int
    ok: int
    altered: int
    not_anchored: int = Field(..., description="Finalized with no metadata anchor")
    unavailable: int = Field(..., description="Unknown: the chain or archive wasn't read")
    problems: list[RecordFinding] = Field(
        default_factory=list, description="Every altered value"
    )


class BackupVerificationResponse(BaseModel):
    checked: int = Field(..., description="Finalized snapshots checked")
    ok: int
    missing: int
    mismatch: int
    unverified: int
    unavailable: int
    repaired: int
    unrecoverable: int
    problems: list[BackupCheck] = Field(
        default_factory=list, description="Every snapshot whose status is not ok"
    )
    live: LiveFileReport = Field(
        ...,
        description="Each document's current signed copy against the chain",
    )
    records: RecordReport = Field(
        ...,
        description="Each document's metadata, snapshot and lifecycle against the chain",
    )
