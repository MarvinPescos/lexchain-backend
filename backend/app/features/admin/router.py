from fastapi import APIRouter, Depends, Query, status, Request
import uuid
from app.infrastructure.security.dependencies import AuthenticatedUser, require_lawyer
from .service import AdminService
from .dependencies import get_admin_service, get_snapshot_archive
from app.features.snapshot.backups import SnapshotArchive
from app.features.snapshot.schemas import BackupVerificationResponse
from .schemas import (
    CreateInvitationRequest, InvitationResponse,
    InvitationListResponse, AdminDashboardResponse, AdminUserListResponse,
    AdminUserResponse, SetUserActiveRequest,
)


router = APIRouter(prefix="/admin", tags=["Admin"])
@router.get(
    "/dashboard",
    response_model=AdminDashboardResponse,
    status_code=status.HTTP_200_OK,
    summary="Admin dashboard overview",
    responses={200: {"description": "Dashboard stats"}, 403: {"description": "Not a lawyer"}},
)


async def get_dashboard(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: AdminService = Depends(get_admin_service),
) -> AdminDashboardResponse:
    return await service.get_dashboard()
@router.get(
    "/users",
    response_model=AdminUserListResponse,
    status_code=status.HTTP_200_OK,
    summary="List all users",
    responses={200: {"description": "List of users"}, 403: {"description": "Not a lawyer"}},
)


async def get_all_users(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_lawyer),
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
        403: {"description": "Not a lawyer"},
    },
)


async def create_invitation(
    request: Request,
    body: CreateInvitationRequest,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: AdminService = Depends(get_admin_service),
) -> InvitationResponse:
    return await service.create_invitation(body.email, body.role, request)
@router.get(
    "/invitations",
    response_model=InvitationListResponse,
    status_code=status.HTTP_200_OK,
    summary="List all invitations",
    responses={200: {"description": "List of invitations"}, 403: {"description": "Not a lawyer"}},
)


async def get_invitations(
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: AdminService = Depends(get_admin_service),
) -> InvitationListResponse:
    return await service.get_invitations()
@router.delete(
    "/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an invitation",
    responses={204: {"description": "Revoked"}, 403: {"description": "Not a lawyer"}, 404: {"description": "Not found"}},
)


async def revoke_invitation(
    invitation_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: AdminService = Depends(get_admin_service),
):
    await service.revoke_invitation(invitation_id)


# Added 2026-09-12 16:59 PST — lets a lawyer actually use the is_active guard.
# get_current_user (dependencies.py) and sign_in both refuse inactive accounts,
# but until now nothing could set the flag, so the check could never fire
# outside of a manual database edit.
@router.patch(
    "/users/{user_id}/active",
    response_model=AdminUserResponse,
    status_code=status.HTTP_200_OK,
    summary="Activate or deactivate a user",
    description=(
        "Deactivating blocks the account on its next request and stops it "
        "signing in. You cannot deactivate yourself or the last active lawyer."
    ),
    responses={
        200: {"description": "Account updated"},
        400: {"description": "Would lock out the last lawyer, or is yourself"},
        403: {"description": "Not a lawyer"},
        404: {"description": "User not found"},
    },
)
async def set_user_active(
    user_id: uuid.UUID,
    body: SetUserActiveRequest,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: AdminService = Depends(get_admin_service),
) -> AdminUserResponse:
    return await service.set_user_active(
        current_user.user_id, user_id, body.is_active
    )


@router.post(
    "/backups/verify",
    response_model=BackupVerificationResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify document backups",
    description=(
        "Downloads every finalized document's archive and checks it against the "
        "hash anchored on-chain. With `repair=true`, a missing archive is written "
        "from the live signed copy, if the snapshot matches the on-chain hash and "
        "the live copy still matches it. A bad archive is reported, never overwritten. "
        "Also runs daily without repair."
    ),
    responses={200: {"description": "Verification summary"}, 403: {"description": "Not a lawyer"}},
)
async def verify_backups(
    repair: bool = Query(False, description="Re-archive missing or bad backups"),
    current_user: AuthenticatedUser = Depends(require_lawyer),
    archive: SnapshotArchive = Depends(get_snapshot_archive),
) -> BackupVerificationResponse:
    return await archive.verify(repair=repair)
