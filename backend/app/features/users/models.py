import uuid
from typing import TYPE_CHECKING

from app.infrastructure.database import Base, TimestampMixin
from app.shared.constants import ROLE_LAWYER, ROLE_USER
from sqlalchemy import Boolean, CheckConstraint, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.features.documents.models import Document, DocumentParty
    from app.features.notifications.models import Notification
    from app.features.books import Book


class User(Base, TimestampMixin):
    __tablename__ = "users"

    # Added 2026-09-12 22:57 PST — constrain role at the database.
    # The allowed roles lived only in Python, so a typo or a direct
    # UPDATE could store a role matching neither check — an account with less
    # authority than a client and no error anywhere to show for it.
    # CHECK rather than a Postgres ENUM: changing the allowed set is one ALTER
    # TABLE instead of an ALTER TYPE dance.
    __table_args__ = (
        CheckConstraint(
            f"role IN ('{ROLE_USER}', '{ROLE_LAWYER}')", name="ck_users_role"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The join key between Supabase Auth and this table: get_current_user
    # looks a user up by this on every authenticated request, which is what
    # `unique=True` (and the index it creates) is carrying.
    supabase_user_id: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    f_name: Mapped[str] = mapped_column(String(255), nullable=False)
    l_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Added 2026-09-12 22:57 PST — dropped `user_metadata`.
    # Answering the question that used to be here: no. Nothing read or wrote
    # it — the only other mention in the repo was a commented-out block in
    # users/repository.py. It was also the one JSON column in a codebase that
    # uses JSONB everywhere else (snapshot, chunks, insights), and it managed
    # to be nullable *and* default to {} at the same time. Re-add it as JSONB
    # with one of those two states if a real use turns up.

    # server_default as well as default: `default=` is applied by SQLAlchemy
    # when it builds the object, so a row inserted from the Supabase dashboard,
    # a migration or raw SQL got nothing and tripped the NOT NULL.
    role: Mapped[str] = mapped_column(
        String(50), default=ROLE_USER, server_default=ROLE_USER, nullable=False
    )
    # Not nullable: a NULL here forced a field_validator in
    # users.schemas.UserProfileResponse, whose own docstring says every user
    # without an avatar would 500 the profile endpoint. The default belongs on
    # the column, where no future response schema can forget it.
    # String(50), not 500 — these are icon identifiers ("icon1"), not URLs.
    avatar: Mapped[str] = mapped_column(
        String(50), default="icon1", server_default="icon1", nullable=False
    )

    # relationship
    documents: Mapped[list["Document"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="[Document.user_id]",
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
