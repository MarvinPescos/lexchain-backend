import uuid
from datetime import datetime, timezone

from app.infrastructure.database.base_repository import BaseRepository
from sqlalchemy import select

from .models import Invitation


class InvitationRepository(BaseRepository[Invitation]):
    def __init__(self, session):
        super().__init__(Invitation, session)

    async def get_by_email(self, email: str) -> Invitation | None:
        result = await self.session.execute(
            select(Invitation).where(Invitation.email == email)
        )
        return result.scalar_one_or_none()

    async def get_pending_by_email(self, email: str) -> Invitation | None:
        result = await self.session.execute(
            select(Invitation).where(
                Invitation.email == email,
                Invitation.status == "pending",
                Invitation.expires_at > datetime.now(timezone.utc),
            )
        )
        return result.scalar_one_or_none()

    async def get_all_pending(self) -> list[Invitation]:
        result = await self.session.execute(
            select(Invitation).where(Invitation.status == "pending")
        )
        return list(result.scalars().all())

    async def claim(self, invitation: Invitation, user_id: uuid.UUID) -> None:
        await self.update(invitation, status="claimed", claimed_by=user_id)
