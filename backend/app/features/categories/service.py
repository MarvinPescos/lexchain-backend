import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.errors.exceptions import NotFoundError

from .repository import CategoryRepository
from .schemas import CategoryResponse, CategoryWriteRequest


class CategoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = CategoryRepository(session)

    async def list_for_user(self, user_id: uuid.UUID) -> list[CategoryResponse]:
        categories = await self.repo.list_for_user(user_id)
        return [CategoryResponse.model_validate(category) for category in categories]

    async def create(
        self, user_id: uuid.UUID, data: CategoryWriteRequest
    ) -> CategoryResponse:
        category = await self.repo.create(
            user_id=user_id, name=data.name, active=True
        )
        await self.session.commit()
        await self.session.refresh(category)
        return CategoryResponse.model_validate(category)

    async def rename(
        self,
        user_id: uuid.UUID,
        category_id: uuid.UUID,
        data: CategoryWriteRequest,
    ) -> CategoryResponse:
        category = await self.repo.get_one(user_id, category_id)
        if category is None:
            raise NotFoundError("Category not found")

        await self.repo.update(category, name=data.name)
        await self.session.commit()
        await self.session.refresh(category)
        return CategoryResponse.model_validate(category)

    async def deactivate(
        self, user_id: uuid.UUID, category_id: uuid.UUID
    ) -> CategoryResponse:
        category = await self.repo.get_one(user_id, category_id)
        if category is None:
            raise NotFoundError("Category not found")

        if category.active:
            await self.repo.update(category, active=False)
            await self.session.commit()
            await self.session.refresh(category)
        return CategoryResponse.model_validate(category)
