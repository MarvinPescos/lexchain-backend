from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from datetime import datetime
from sqlalchemy import DateTime, func

class Base(DeclarativeBase):
    """
        Base class for all database models.
        All model should inherit from this.
    """
    ...

class TimestampMixin:
    """Mixin to add created_at and updated_at to models"""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now(), default=None)

