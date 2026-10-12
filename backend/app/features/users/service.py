import uuid

from app.shared.errors.exceptions import NotFoundError
from sqlalchemy.ext.asyncio import AsyncSession

from .repository import UserRepository
from .schemas import UserProfileResponse, UserSearchResponse


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = UserRepository(session)

    async def search_user(self, email: str) -> UserSearchResponse:
        """Search for a user by email.

        Args:
            email: The email address to look up.

        Returns:
            UserSearchResponse with the user's id, email, f_name, and l_name.

        Raises:
            NotFoundError: If no user with the given email exists.
        """
        user = await self.repo.get_by_email(email)
        if not user:
            raise NotFoundError("User not found")
        return UserSearchResponse(
            user_id=user.id,
            email=user.email,
            f_name=user.f_name,
            l_name=user.l_name,
        )

    async def get_user_profile(self, user_id: uuid.UUID) -> UserProfileResponse:

        user = await self.repo.get_by_id(user_id)
        if not user:
            raise NotFoundError("User not found")

        return UserProfileResponse(
            email=user.email,
            f_name=user.f_name,
            l_name=user.l_name,
            avatar=user.avatar,
            role=user.role,
        )
