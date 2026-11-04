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


BackupStatus = Literal["ok", "missing", "mismatch", "repaired", "unrecoverable"]


class BackupCheck(BaseModel):
    document_id: uuid.UUID
    status: BackupStatus = Field(
        ...,
        description=(
            "ok: archive present and matches the anchored hash. missing: no "
            "archive. mismatch: the archive's bytes differ from the anchored "
            "hash. repaired: re-archived from the live copy. unrecoverable: no "
            "good archive and the live copy no longer matches either."
        ),
    )
    backup_key: str | None = None


class BackupVerificationResponse(BaseModel):
    checked: int = Field(..., description="Finalized snapshots checked")
    ok: int
    missing: int
    mismatch: int
    repaired: int
    unrecoverable: int
    problems: list[BackupCheck] = Field(
        default_factory=list, description="Every snapshot whose status is not ok"
    )
