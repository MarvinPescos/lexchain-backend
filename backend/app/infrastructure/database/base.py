import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """
    Base class for all database models.
    All model should inherit from this.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


# Added 2026-09-12 23:20 PST — declares what "scoped to a user" means.
# UserScopeRepository filters on `self.model.user_id`, but BaseRepository's
# TypeVar is bound to Base, which promises no such column — so the filter
# needed a `# type: ignore` and a mismatch could only surface at runtime.
# UserRepository(UserScopeRepository[User]) was exactly that: User has
# supabase_user_id, not user_id, so get_one/get_many raised AttributeError.
#
# Revised 2026-09-13 — an abstract Base subclass, not a bare mixin.
# The repository needs a bound meaning "a Base *and* user-owned", and Python
# has no intersection type; `bound=UserOwned | Base` says *either*, which
# satisfies neither requirement (the Base arm has no user_id, the UserOwned arm
# is not a Base). An abstract declarative base is the standard way to express
# the conjunction — it is a Base, and it carries the column.
#
# Owning the column here also deduplicates it: Document and Book declared it
# byte-identically.
class UserOwned(Base):
    """Abstract base for tables scoped to an owning user."""

    __abstract__ = True

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )


class TimestampMixin:
    """Mixin to add created_at and updated_at to models"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), default=None
    )
