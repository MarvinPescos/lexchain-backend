import uuid
from typing import TYPE_CHECKING

from app.infrastructure.database import Base, TimestampMixin
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.features.documents.models import Document

class DocumentSnapshot(Base, TimestampMixin):
    """The signed copy as it was frozen at finalize. Written once, never updated.

    `file_sha256` is the hash anchored on-chain: the final PDF bytes, not text.
    """

    __tablename__ = "document_snapshots"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), unique=True
    )
    signed_copy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signed_copies.id", ondelete="RESTRICT")
    )
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # relationship

    document: Mapped["Document"] = relationship(back_populates="snapshot")
