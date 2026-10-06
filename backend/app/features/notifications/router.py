import uuid

from app.infrastructure.security.dependencies import AuthenticatedUser, require_auth
from app.shared.errors.exceptions import NotFoundError
from fastapi import APIRouter, Depends, Query, status

from .dependencies import get_notification_service
from .schemas import MarkAllReadResponse, NotificationListResponse, UnreadCountResponse
from .service import NotificationService

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get(
    "/",
    response_model=NotificationListResponse,
    status_code=status.HTTP_200_OK,
    summary="List notifications for current user",
    responses={
        200: {"description": "Notifications retrieved"},
        401: {"description": "Not authenticated"},
    },
)
async def list_notifications(
    current_user: AuthenticatedUser = Depends(require_auth),
    service: NotificationService = Depends(get_notification_service),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
) -> NotificationListResponse:
    return await service.list_for_user(current_user.user_id, limit, offset, unread_only)


@router.get(
    "/unread-count",
    response_model=UnreadCountResponse,
    status_code=status.HTTP_200_OK,
    summary="Get unread notification count",
    responses={
        200: {"description": "Unread count"},
        401: {"description": "Not authenticated"},
    },
)
async def get_unread_count(
    current_user: AuthenticatedUser = Depends(require_auth),
    service: NotificationService = Depends(get_notification_service),
) -> UnreadCountResponse:
    return await service.get_unread_count(current_user.user_id)


@router.patch(
    "/{notification_id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark notification as read",
    responses={
        204: {"description": "Marked as read"},
        401: {"description": "Not authenticated"},
        404: {"description": "Not found or not yours"},
    },
)
async def mark_read(
    notification_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_auth),
    service: NotificationService = Depends(get_notification_service),
):
    ok = await service.mark_read(current_user.user_id, notification_id)
    if not ok:
        raise NotFoundError("Notification not found")


@router.patch(
    "/read-all",
    response_model=MarkAllReadResponse,
    status_code=status.HTTP_200_OK,
    summary="Mark all notifications as read",
    responses={
        200: {"description": "All marked read"},
        401: {"description": "Not authenticated"},
    },
)
async def mark_all_read(
    current_user: AuthenticatedUser = Depends(require_auth),
    service: NotificationService = Depends(get_notification_service),
) -> MarkAllReadResponse:
    count = await service.mark_all_read(current_user.user_id)
    return MarkAllReadResponse(marked_read=count)
