from app.infrastructure.database import get_db
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from .service import BookService


def get_book_service(
    db: AsyncSession = Depends(get_db),
) -> BookService:
    """Dependency injection (provides BookService with database session)"""
    return BookService(db)
