from typing import Optional

from app.features.users.models import User
from app.infrastructure.database import BaseRepository
from app.infrastructure.observability.logging_setup import log
from app.shared.errors.exceptions import NotFoundError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


# Changed 2026-09-12 23:20 PST — BaseRepository, resolving the TODO that was
# here. This extended UserScopeRepository[User], whose get_one/get_many filter
# on model.user_id — a column User does not have (it has supabase_user_id), so
# either method raised AttributeError. Nothing called them, which is the only
# reason it never surfaced. Users are not scoped to an owning user; they are
# the thing being owned.
class UserRepository(BaseRepository[User]):
    """Repository pattern for user data access"""

    def __init__(self, session: AsyncSession):
        super().__init__(User, session)

    # ======Custom Methods======

    async def get_by_supabase_id(self, supabase_user_id: str) -> Optional[User]:
        """Get user by supabase user id"""

        result = await self.session.execute(
            select(User).where(User.supabase_user_id == supabase_user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def count_by_role(self, role: str) -> int:
        """Count users by role."""
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count()).select_from(self.model).where(self.model.role == role)
        )
        return result.scalar() or 0

    # Added 2026-09-12 16:59 PST — supports the last-lawyer guard on
    # AdminService.set_user_active. count_by_role counts deactivated accounts
    # too, which would let the final working lawyer be switched off as long as
    # a disabled one existed.
    async def count_active_by_role(self, role: str) -> int:
        """Count active users holding `role`."""
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count())
            .select_from(self.model)
            .where(self.model.role == role, self.model.is_active.is_(True))
        )
        return result.scalar() or 0

    async def sync_from_supabase(
        self, supabase_user_id: str, email: str, **additional_data
    ) -> User:
        user = await self.get_by_supabase_id(supabase_user_id)
        if user:
            return await self.update(user, email=email, **additional_data)
        # New user — check for pending invitation
        from app.features.admin.repository import InvitationRepository

        inv_repo = InvitationRepository(self.session)
        invitation = await inv_repo.get_pending_by_email(email)
        role = invitation.role if invitation else "user"
        new_user = await self.create(
            supabase_user_id=supabase_user_id,
            email=email,
            role=role,
            **additional_data,
        )
        if invitation:
            await inv_repo.claim(invitation, new_user.id)
            await self.session.flush()
        return new_user

    # TODO Review this part later.

    # For OAuth (Google, Facebook, etc.)
    # async def get_or_create_from_token(self, payload: dict) -> User:
    #     """
    #     Get User from DB, or create them if they don't exist
    #     """

    #     supabase_user_id = payload.get('sub')
    #     email = payload.get('email')

    #      # check if user exist locally (in db)
    #     user = await self.get_by_supabase_id(supabase_user_id)

    #     if user:
    #         return user #Already synced return existing

    #     # get the user's metadata if user not exist yet (Para ra ni sa ako! haha.)
    #     meta = payload.get("user_metadata", {})
    #     username = meta.get("username") or meta.get("name") or email.split("@")[0]

    #     log.info(f"JIT Creating user from token: {email}")

    #     return await self.create(
    #         supabase_user_id=supabase_user_id,
    #         email=email,
    #         username=username,
    #         is_email_verified=True
    #     )
