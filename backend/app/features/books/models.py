import uuid
from typing import TYPE_CHECKING

from app.infrastructure.database import Base, TimestampMixin
from sqlalchemy import Boolean, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.features.documents import Document
    from app.features.users import User


class Book(Base, TimestampMixin):
    __tablename__ = "books"

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
    book_number: Mapped[int] = mapped_column(Integer, nullable=False)
    series_year: Mapped[int] = mapped_column(Integer, nullable=False)
    document_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_full: Mapped[bool] = mapped_column(Boolean, default=False)

    # === relationship ===
    user: Mapped["User"] = relationship(back_populates="books")
    documents: Mapped[list["Document"]] = relationship(
        back_populates="book", cascade="all, delete-orphan"
    )
