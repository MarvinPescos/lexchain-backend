import uuid

from sqlalchemy import select

from app.infrastructure.database import UserScopeRepository

from .models import Category


class CategoryRepository(UserScopeRepository[Category]):
    def __init__(self, session):
        super().__init__(Category, session)

    async def list_for_user(self, user_id: uuid.UUID) -> list[Category]:
        result = await self.session.execute(
            select(Category)
            .where(Category.user_id == user_id)
            .order_by(Category.created_at, Category.id)
        )
        return list(result.scalars().all())
