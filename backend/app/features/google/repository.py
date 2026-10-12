import uuid

from app.infrastructure.database import BaseRepository
from sqlalchemy import select

from .models import GoogleConnection


class GoogleConnectionRepository(BaseRepository[GoogleConnection]):
    def __init__(self, session):
        super().__init__(GoogleConnection, session)

    async def get_for_user(self, user_id: uuid.UUID) -> GoogleConnection | None:
        result = await self.session.execute(
            select(GoogleConnection).where(GoogleConnection.user_id == user_id)
        )
        return result.scalar_one_or_none()
