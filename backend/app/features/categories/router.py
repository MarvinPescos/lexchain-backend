import uuid

from fastapi import APIRouter, Depends, status

from app.infrastructure.security.dependencies import AuthenticatedUser, require_lawyer

from .dependencies import get_category_service
from .schemas import CategoryResponse, CategoryWriteRequest
from .service import CategoryService

router = APIRouter(prefix="/categories", tags=["Categories"])


@router.get(
    "/",
    response_model=list[CategoryResponse],
    status_code=status.HTTP_200_OK,
    summary="List document categories",
    description="Return active and inactive categories owned by the authenticated lawyer.",
    responses={
        200: {"description": "Categories in creation order"},
        401: {"description": "Not authenticated"},
        403: {"description": "Only lawyers may manage categories"},
    },
)
async def list_categories(
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: CategoryService = Depends(get_category_service),
) -> list[CategoryResponse]:
    return await service.list_for_user(current_user.user_id)


@router.post(
    "/",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a document category",
    responses={
        201: {"description": "Category created"},
        401: {"description": "Not authenticated"},
        403: {"description": "Only lawyers may manage categories"},
        422: {"description": "Name must contain 1 to 100 characters"},
    },
)
async def create_category(
    body: CategoryWriteRequest,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: CategoryService = Depends(get_category_service),
) -> CategoryResponse:
    return await service.create(current_user.user_id, body)


@router.patch(
    "/{category_id}",
    response_model=CategoryResponse,
    status_code=status.HTTP_200_OK,
    summary="Rename a document category",
    responses={
        200: {"description": "Category updated"},
        401: {"description": "Not authenticated"},
        403: {"description": "Only lawyers may manage categories"},
        404: {"description": "Category not found"},
        422: {"description": "Name must contain 1 to 100 characters"},
    },
)
async def rename_category(
    category_id: uuid.UUID,
    body: CategoryWriteRequest,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: CategoryService = Depends(get_category_service),
) -> CategoryResponse:
    return await service.rename(current_user.user_id, category_id, body)


@router.post(
    "/{category_id}/deactivate",
    response_model=CategoryResponse,
    status_code=status.HTTP_200_OK,
    summary="Deactivate a document category",
    description="Keep the category saved in the directory while marking it inactive.",
    responses={
        200: {"description": "Category deactivated; safe to retry"},
        401: {"description": "Not authenticated"},
        403: {"description": "Only lawyers may manage categories"},
        404: {"description": "Category not found"},
    },
)
async def deactivate_category(
    category_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: CategoryService = Depends(get_category_service),
) -> CategoryResponse:
    return await service.deactivate(current_user.user_id, category_id)
