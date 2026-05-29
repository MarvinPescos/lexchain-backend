import uuid

from app.infrastructure.observability.logging_setup import log

from .models import NotificationType
from .repository import NotificationRepository
from .schemas import NotificationListResponse, NotificationResponse, UnreadCountResponse


class NotificationService:
    def __init__(self, session):
        self.repo = NotificationRepository(session)

    async def create(
        self,
        user_id: uuid.UUID,
        type: NotificationType,
        title: str,
        body: str,
        event_metadata: dict | None = None,
    ):
        notification = await self.repo.create(
            user_id=user_id,
            type=type,
            title=title,
            body=body,
            event_metadata=event_metadata or {},
        )
        log.info(
            "notification.created",
            user_id=str(user_id),
            type=type.value,
        )
        return notification

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
    ) -> NotificationListResponse:
        notifications = await self.repo.get_for_user(
            user_id, limit, offset, unread_only
        )
        return NotificationListResponse(
            notifications=[
                NotificationResponse.model_validate(n) for n in notifications
            ],
            total=len(notifications),
        )

    async def get_unread_count(self, user_id: uuid.UUID) -> UnreadCountResponse:
        unread = await self.repo.get_unread_count(user_id)
        return UnreadCountResponse(unread=unread)

    async def mark_read(self, user_id: uuid.UUID, notification_id: uuid.UUID) -> bool:
        return await self.repo.mark_read(notification_id, user_id)

    async def mark_all_read(self, user_id: uuid.UUID) -> int:
        return await self.repo.mark_all_read(user_id)
