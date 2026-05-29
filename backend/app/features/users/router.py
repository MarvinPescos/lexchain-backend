from app.features.auth.schemas import UserProfileResponse
from app.infrastructure.security.dependencies import AuthenticatedUser, require_auth
from fastapi import APIRouter, Depends, Query, status

from .dependencies import get_user_service
from .models import User
from .schemas import UserSearchResponse
from .service import UserService

router = APIRouter(prefix="/users", tags=["Users"])


@router.get(
    "/search",
    response_model=UserSearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Search for a user by email",
    responses={
        200: {"description": "User found successfully"},
        401: {"description": "User is not authenticated"},
        404: {
            "description": "User not found",
            "content": {"application/json": {"example": {"detail": "User not found"}}},
        },
    },
)
async def search_user(
    email: str = Query(..., description="Email to search for"),
    _: User = Depends(require_auth),
    service: UserService = Depends(get_user_service),
) -> UserSearchResponse:
    """
    Search for a user by email.

    Args:
        email: The email address to look up.
        current_user: The authenticated user making the request.
        service: The user management service.

    Returns:
        `UserSearchResponse` containing the user's id, email, f_name, and l_name.
    """
    return await service.search_user(email)


@router.get(
    "/",
    response_model=UserProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Get user profile",
    responses={
        200: {"description": "User found successfully"},
        401: {"description": "User is not authenticated"},
        404: {
            "description": "User not found",
            "content": {"application/json": {"example": {"detail": "User not found"}}},
        },
    },
)
async def get_user_profile(
    current_user: AuthenticatedUser = Depends(require_auth),
    service: UserService = Depends(get_user_service),
) -> UserProfileResponse:
    """
    Search for a user by email.

    Args:
        email: The email address to look up.
        current_user: The authenticated user making the request.
        service: The user management service.

    Returns:
        `UserSearchResponse` containing the user's id, email, f_name, and l_name.
    """
    return await service.get_user_profile(current_user.user_id)
