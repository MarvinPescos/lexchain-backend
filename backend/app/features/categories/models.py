from sqlalchemy import Boolean, Index, String, true
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import TimestampMixin, UserOwned


class Category(UserOwned, TimestampMixin):
    __tablename__ = "categories"
    __table_args__ = (Index("ix_categories_user_id", "user_id"),)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
