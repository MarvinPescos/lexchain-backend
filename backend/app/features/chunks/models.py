import uuid
from typing import TYPE_CHECKING, Any

from app.infrastructure.database import Base, TimestampMixin
from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.features.documents import Document


class DocumentChunk(Base, TimestampMixin):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint(
            "extraction_id", "chunk_index", name="uq_chunk_extraction_index"
        ),
    )

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
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_extraction.id", ondelete="CASCADE"),
        nullable=False,
    )

    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # Embedding stored as JSONB for now; swap to pgvector later.
    # For the sake of the pipeline JSONB sa
    embedding: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    embedding_vec: Mapped[list[float] | None] = mapped_column(
        Vector(384), nullable=True
    )

    strategy: Mapped[str] = mapped_column(String(50), nullable=False)
    engine: Mapped[str] = mapped_column(String(50), nullable=False)
    engine_version: Mapped[str | None] = mapped_column(String(50), nullable=True)

    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # relationship
    document: Mapped["Document"] = relationship(back_populates="chunks")
