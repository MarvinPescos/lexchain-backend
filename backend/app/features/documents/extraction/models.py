import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database import Base, TimestampMixin

if TYPE_CHECKING:
    from app.features.documents import Document
    from app.features.users.models import User


class DocumentExtraction(Base, TimestampMixin):
    """Raw OCR output plus the issuer's reviewed correction of it.

    `text` is what the OCR engine produced and is never mutated — it is the
    evidence of what the machine read. Issuer corrections land in `blocks` /
    `reviewed_text`, so any later dispute can show exactly what was changed.
    """

    __tablename__ = "document_extraction"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False
    )
    engine: Mapped[str] = mapped_column(String(50), nullable=False)
    engine_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # === Issuer review ===
    # Blocks are the unit of truth once a review starts: each carries the OCR
    # text, the issuer's edit, and the bbox/page it came from, so a flag can
    # point at a rectangle on a page and edits stay locatable.
    blocks: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True
    )
    flags: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True
    )
    reviewed_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    edited_block_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    document: Mapped["Document"] = relationship(back_populates="extractions")
    reviewer: Mapped["User | None"] = relationship(foreign_keys=[reviewed_by])

    @property
    def approved_text(self) -> str:
        """Text the rest of the pipeline should use: the review if one exists."""
        return self.reviewed_text if self.reviewed_text is not None else self.text
