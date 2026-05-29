from fastapi import APIRouter, Depends, status, Request
import uuid
from app.infrastructure.security.dependencies import AuthenticatedUser, require_admin
from .service import AdminService
from .dependencies import get_admin_service
from .schemas import (
    CreateInvitationRequest, InvitationResponse,
    InvitationListResponse, AdminDashboardResponse, AdminUserListResponse,
)


router = APIRouter(prefix="/admin", tags=["Admin"])
@router.get(
    "/dashboard",
    response_model=AdminDashboardResponse,
    status_code=status.HTTP_200_OK,
    summary="Admin dashboard overview",
    responses={200: {"description": "Dashboard stats"}, 401: {"description": "Not admin"}},
)


async def get_dashboard(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_admin),
    service: AdminService = Depends(get_admin_service),
) -> AdminDashboardResponse:
    return await service.get_dashboard()
@router.get(
    "/users",
    response_model=AdminUserListResponse,
    status_code=status.HTTP_200_OK,
    summary="List all users",
    responses={200: {"description": "List of users"}, 401: {"description": "Not admin"}},
)


async def get_all_users(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_admin),
    service: AdminService = Depends(get_admin_service),
) -> AdminUserListResponse:
    return await service.get_all_users()
@router.post(
    "/invitations",
    response_model=InvitationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create lawyer invitation",
    description="Create a magic-link invitation. Share the link with the lawyer to sign up.",
    responses={
        201: {"description": "Invitation created"},
        400: {"description": "Invitation already exists"},
        401: {"description": "Not admin"},
    },
)


async def create_invitation(
    request: Request,
    body: CreateInvitationRequest,
    current_user: AuthenticatedUser = Depends(require_admin),
    service: AdminService = Depends(get_admin_service),
) -> InvitationResponse:
    return await service.create_invitation(body.email, body.role, request)
@router.get(
    "/invitations",
    response_model=InvitationListResponse,
    status_code=status.HTTP_200_OK,
    summary="List all invitations",
    responses={200: {"description": "List of invitations"}, 401: {"description": "Not admin"}},
)


async def get_invitations(
    current_user: AuthenticatedUser = Depends(require_admin),
    service: AdminService = Depends(get_admin_service),
) -> InvitationListResponse:
    return await service.get_invitations()
@router.delete(
    "/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an invitation",
    responses={204: {"description": "Revoked"}, 401: {"description": "Not admin"}, 404: {"description": "Not found"}},
)


async def revoke_invitation(
    invitation_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_admin),
    service: AdminService = Depends(get_admin_service),
):
    await service.revoke_invitation(invitation_id)