from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import String, ForeignKey, BigInteger
from sqlalchemy.dialects.postgresql import UUID 
from typing import TYPE_CHECKING
import uuid

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



