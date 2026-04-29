from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import String, Boolean, JSON
from typing import Any, TYPE_CHECKING
import uuid

from app.infrastructure.database import Base, TimestampMixin

if (TYPE_CHECKING):
    from app.features.documents.models import Document

class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    supabase_user_id: Mapped[str] = mapped_column(String(255), unique=True ,nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    f_name: Mapped[str] = mapped_column(String(255), nullable=False)
    l_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    user_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    avatar: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # relationship
    documents: Mapped[list["Document"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan"
    )
 
    def __repr__(self):
        return f"<User {self.email}>"
