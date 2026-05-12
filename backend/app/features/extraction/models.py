from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import String, ForeignKey, Text, Integer, Float
from sqlalchemy.dialects.postgresql import UUID, JSONB
from typing import Any, TYPE_CHECKING
import uuid

from app.infrastructure.database import Base, TimestampMixin

if (TYPE_CHECKING):
    from app.features.documents import Document


class DocumentExtraction(Base, TimestampMixin):
    """"""

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
    engine_version: Mapped[str] = mapped_column(String(50), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_avg: Mapped[float] = mapped_column(Float, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    document: Mapped["Document"] = relationship(back_populates="extractions")

