from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import UserScopeRepository
from app.infrastructure.observability.logging_setup import log
from app.features.users.models import User
from app.shared.errors.exceptions import NotFoundError

# TODO Switch to based or make a custom repo for it
class UserRepository(UserScopeRepository[User]):
    """Repository pattern for user data access"""

    def __init__(self, session: AsyncSession):
        super().__init__(User, session)

    # ======Custom Methods======

    async def get_by_supabase_id(self, supabase_user_id: str) -> Optional[User]:
        """Get user by supabase user id"""
        
        result = await self.session.execute(
            select(User).
            where(User.supabase_user_id == supabase_user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User:
        result = await self.session.execute(
            select(User).
            where(User.email == email)
        )
        if not result:
            raise NotFoundError("No user in this email")
        return result.scalar_one_or_none()

    async def sync_from_supabase(
        self,
        supabase_user_id: str,
        email: str, 
        **additional_data
    ) -> User:
        """
           Sync user from supabase (upsert logic) 
           Create if doesn't exist, updates if exists
        """

        # Check if user exist locally (db)
        user = await self.get_by_supabase_id(supabase_user_id)

        #If exist update else create, upsert logic
        if user: 
            return await self.update(user, email=email, **additional_data) 
        else:   
            return await self.create(supabase_user_id=supabase_user_id, email=email, **additional_data)
    
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



