from app.infrastructure.database import UserScopeRepository
from sqlalchemy import select
import uuid

from .models import Book


class BookRepository(UserScopeRepository[Book]):
    """Repository pattern for Document"""

    def __init__(self, session):
        super().__init__(Book, session)

    # === Custom methods ===

    async def get_by_number_and_year(
        self, user_id: uuid.UUID, book_number: int, series_year: int
    ) -> Book | None:
        """Find a book by number and year scoped to the user."""
        stmt = select(Book).where(
            Book.user_id == user_id,
            Book.book_number == book_number,
            Book.series_year == series_year,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_book_for_year(
        self, user_id: uuid.UUID, series_year: int
    ) -> Book | None:
        """Find any non-full book for a specific year scoped to the user."""
        stmt = select(Book).where(
            Book.user_id == user_id,
            Book.series_year == series_year,
            Book.is_full == False,
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()
