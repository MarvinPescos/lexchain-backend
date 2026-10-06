import secrets
import uuid
from datetime import datetime, timezone

from app.infrastructure.database.base_repository import BaseRepository
from sqlalchemy import select

from .models import INVITATION_TTL, Invitation


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

    # Added 2026-09-07 00:01 PST — re-inviting an address must reuse its row.
    # Invitation.email is unique, so a second create() for the same address is
    # an IntegrityError. Returns a stale (claimed/expired) invitation to pending
    # with a fresh token and expiry so the old link stops working.
    async def reissue(self, invitation: Invitation, role: str) -> Invitation:
        """Return a stale invitation to pending with a new token and expiry."""
        return await self.update(
            invitation,
            role=role,
            status="pending",
            token=secrets.token_hex(32),
            expires_at=datetime.now(timezone.utc) + INVITATION_TTL,
            claimed_by=None,
        )
