import uuid
from typing import List

from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError

from ..observability.logging_setup import log
from .base_repository import BaseRepository, ModelType
from .exceptions import DatabaseError


class UserScopeRepository(BaseRepository[ModelType]):
    def _build_conditions(
        self, user_id: uuid.UUID, identifier: str | uuid.UUID | None = None, **kwargs
    ) -> list:
        conditions = [self.model.user_id == user_id]  # type: ignore

        if identifier:
            try:
                parsed = uuid.UUID(str(identifier))
                conditions.append(self.model.id == parsed)
            except ValueError:
                # TODO check later if slug still needed or just straight up encrypt url
                conditions.append(self.model.slug == identifier)  # type: ignore

        return conditions

    async def get_one(
        self, user_id: uuid.UUID, identifier: str | uuid.UUID | None = None, **kwargs
    ) -> ModelType | None:
        """
        Get single item by identifier, optionally by user.

        Args:
            user_id: scope to this user
            identifier: the identifier being used.
        """
        try:
            conditions = self._build_conditions(
                user_id=user_id,
                identifier=identifier,
                **kwargs,
            )
            result = await self.session.execute(
                select(self.model).where(and_(*conditions))
            )

            return result.scalar_one_or_none()

        except SQLAlchemyError as e:
            log.error(
                "database.error",
                model=self.model_name,
                operation="get_one",
                error=str(e),
            )
            raise DatabaseError(f"Failed to get {self.model_name}", original_error=e)

    async def get_many(
        self, user_id: uuid.UUID, limit: int = 100, offset: int = 0, **kwargs
    ) -> List[ModelType]:
        """ """
        try:
            conditions = self._build_conditions(
                user_id=user_id, identifier=None, **kwargs
            )
            result = await self.session.execute(
                select(self.model).where(and_(*conditions)).limit(limit).offset(offset)
            )
            return list(result.scalars().all())
        except SQLAlchemyError as e:
            log.error(
                "database.error",
                model=self.model_name,
                operations="get_many",
                error=str(e),
            )
            raise DatabaseError(f"Failed to list {self.model_name}", original_error=e)
