import uuid
from typing import List, TypeVar

from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError

from ..observability.logging_setup import log
from .base import UserOwned
from .base_repository import BaseRepository
from .exceptions import DatabaseError

# Changed 2026-09-12 23:20 PST — bound to UserOwned, not Base.
# BaseRepository's ModelType is bound to Base, which guarantees nothing about a
# user_id column, so every filter here needed a `# type: ignore` and a model
# without the column failed only at runtime. UserOwned is an abstract Base
# subclass (see base.py), so this bound satisfies BaseRepository's own bound
# *and* guarantees the column — which a `UserOwned | Base` union did not.
UserOwnedModel = TypeVar("UserOwnedModel", bound=UserOwned)


class UserScopeRepository(BaseRepository[UserOwnedModel]):
    def _build_conditions(
        self, user_id: uuid.UUID, identifier: uuid.UUID | None = None, **kwargs
    ) -> list:
        # Changed 2026-09-12 23:20 PST — dropped the slug fallback.
        # `identifier` used to accept a str: anything that did not parse as a
        # UUID was treated as a slug and matched against self.model.slug. No
        # model in this codebase has a slug column, so that branch could only
        # ever raise AttributeError, and every caller already passes a UUID
        # (book_id / document_id arrive typed from the routers). Narrowing the
        # parameter removes the branch, the try/except and both type: ignores.
        conditions = [self.model.user_id == user_id]

        if identifier:
            conditions.append(self.model.id == identifier)

        return conditions

    async def get_one(
        self, user_id: uuid.UUID, identifier: uuid.UUID | None = None, **kwargs
    ) -> UserOwnedModel | None:
        """
        Get a single item owned by this user.

        Args:
            user_id: scope to this user
            identifier: the row's id, when narrowing to one row.
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
    ) -> List[UserOwnedModel]:
        """Get a page of the items owned by this user."""
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
                operation="get_many",
                error=str(e),
            )
            raise DatabaseError(f"Failed to list {self.model_name}", original_error=e)
