import uuid
from typing import TYPE_CHECKING, Any

from app.infrastructure.database import Base, TimestampMixin
from sqlalchemy import JSON, Boolean, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.features.documents.models import Document, DocumentParty
    from app.features.notifications.models import Notification
    from app.features.books import Book


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # What's the point of supabase user_id?
    supabase_user_id: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    f_name: Mapped[str] = mapped_column(String(255), nullable=False)
    l_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Do i even need metadata?? I already have role and avatar.
    user_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, default=dict
    )
    role: Mapped[str] = mapped_column(String(50), default="user", nullable=False)
    avatar: Mapped[str] = mapped_column(String(500), nullable=True)

    # relationship
    documents: Mapped[list["Document"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    parties: Mapped[list["DocumentParty"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    books: Mapped[list["Book"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<User {self.email}>"
