import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from app.infrastructure.database import Base, TimestampMixin
from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.features.blockchain.models import OnChainRecord
    from app.features.books import Book
    from app.features.chunks import DocumentChunk
    from app.features.extraction.models import DocumentExtraction
    from app.features.insights.models import DocumentInsight
    from app.features.users.models import User


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_url: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    on_chain: Mapped[bool] = mapped_column(Boolean, default=False)
    document_hash: Mapped[str] = mapped_column(String(500), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # previous_document_id: Mapped[uuid.UUID] = mapped_column( # removed: no need
    #     UUID(as_uuid=True),
    #     ForeignKey("documents.id", ondelete="SET NULL"),
    #     nullable=True,
    # )
    finalized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=func.now()
    )
    finalized_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    book_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("books.id", ondelete="CASCADE"),
        nullable=False,
    )
    # is_latest: Mapped[bool] = mapped_column(Boolean, default=True) # removed: no need

    # === relationship ===
    user: Mapped["User"] = relationship(
        back_populates="documents", foreign_keys=[user_id]
    )
    book: Mapped["Book"] = relationship(back_populates="documents")
    extractions: Mapped[list["DocumentExtraction"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )
    insights: Mapped[list["DocumentInsight"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )
    # Inside class Document, add:
    on_chain_record: Mapped["OnChainRecord | None"] = relationship(
        back_populates="document",
        uselist=False,
    )
    parties: Mapped[list["DocumentParty"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    # previous_document: Mapped["Document | None"] = relationship(
    #     "Document",
    #     remote_side=[id],
    #     foreign_keys=[previous_document_id],
    #     back_populates="next_document",
    #     uselist=False,
    # )
    # next_document: Mapped["Document | None"] = relationship(
    #     "Document",
    #     remote_side=[previous_document_id],
    #     foreign_keys=[previous_document_id],
    #     back_populates="previous_document",
    #     uselist=False,
    # )


# ========


class DocumentParty(Base, TimestampMixin):
    __tablename__ = "document_parties"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), default="viewer")

    # relasyon haha
    document: Mapped["Document"] = relationship(back_populates="parties")
    user: Mapped["User"] = relationship(back_populates="parties")

    __table_args__ = (
        UniqueConstraint("document_id", "user_id", name="uq_document_party"),
    )
