import uuid
from datetime import datetime
from typing import List, cast

from app.infrastructure.database.base_repository import BaseRepository
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Notification, NotificationType


class NotificationRepository(BaseRepository[Notification]):
    def __init__(self, session: AsyncSession):
        super().__init__(Notification, session)

    async def get_for_user(
        self,
        user_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
    ) -> List[Notification]:
        stmt = select(Notification).where(Notification.user_id == user_id)
        if unread_only:
            stmt = stmt.where(Notification.is_read.is_(False))
        stmt = stmt.order_by(Notification.created_at.desc()).limit(limit).offset(offset)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_metadata_by_type(self, type: NotificationType) -> list[dict]:
        result = await self.session.execute(
            select(Notification.event_metadata).where(Notification.type == type)
        )
        return list(result.scalars().all())

    async def get_sent_by_type(
        self, type: NotificationType
    ) -> list[tuple[dict, datetime]]:
        result = await self.session.execute(
            select(Notification.event_metadata, Notification.created_at).where(
                Notification.type == type
            )
        )
        return list(result.tuples().all())

    async def get_unread_count(self, user_id: uuid.UUID) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.is_read.is_(False))
        )
        return result.scalar() or 0

    async def mark_read(self, notification_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        result = await self.session.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
            )
        )
        notification = result.scalar_one_or_none()
        if not notification:
            return False
        await self.update(notification, is_read=True)
        return True

    async def mark_all_read(self, user_id: uuid.UUID) -> int:
        result = await self.session.execute(
            update(Notification)
            .where(Notification.user_id == user_id, Notification.is_read.is_(False))
            .values(is_read=True)
        )
        await self.session.flush()
        return cast(CursorResult, result).rowcount or 0
