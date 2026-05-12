from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import String, ForeignKey, Text, Float
from sqlalchemy.dialects.postgresql import UUID, JSONB
from typing import Any, TYPE_CHECKING
import uuid

from app.infrastructure.database import Base, TimestampMixin

if TYPE_CHECKING:
    from app.features.documents.models import Document
    from app.features.extraction.models import DocumentExtraction

class DocumentInsight(Base, TimestampMixin):
    __tablename__ = "document_insight"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    extraction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_extraction.id", ondelete="SET NULL"),
        nullable=True,
    )
    engine: Mapped[str] = mapped_column(String(50), nullable=False)
    engine_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    labels: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    entities: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    risk_flags: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    document: Mapped["Document"] = relationship(back_populates="insights")