import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database import Base, TimestampMixin

if TYPE_CHECKING:
    from app.features.documents import Document

class OnChainRecord(Base, TimestampMixin):
    __tablename__ = "on_chain_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    tx_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    onchain_document_id: Mapped[str] = mapped_column(String(255), nullable=False)
    data_hash: Mapped[str] = mapped_column(String(500), nullable=False)
    onchain_timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    issued_by: Mapped[str] = mapped_column(String(42), nullable=False)

    # relationships
    document: Mapped["Document"] = relationship(back_populates="on_chain_record")


class VerificationResult(Base):
    """One completed check of a document against the chain, kept for reporting.

    Written per attempt, never updated, so repeated checks stay separate rows.
    Holds the verdict and the hashes that explain it — no file bytes or text.
    """

    __tablename__ = "verification_results"
    __table_args__ = (Index("ix_verification_results_verified_at_id", "verified_at", "id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_authentic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    baseline_trusted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    onchain_hash: Mapped[str | None] = mapped_column(String(500), nullable=True)
    snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    tamper_report_status: Mapped[str] = mapped_column(String(16), nullable=False)
