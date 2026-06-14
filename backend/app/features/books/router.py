import uuid
from typing import List

from app.infrastructure.security.dependencies import (
    AuthenticatedUser,
    require_lawyer,
)
from fastapi import APIRouter, Depends, Request, status

from .dependencies import get_book_service
from .schemas import BookCreateRequest, BookResponse
from .service import BookService

router = APIRouter(prefix="/books", tags=["Books"])


# ── List ────────────────────────────────────────────────────────────

@router.get(
    "/",
    response_model=List[BookResponse],
    status_code=status.HTTP_200_OK,
    summary="List all books",
    description="Return all books belonging to the authenticated lawyer.",
    responses={
        200: {"description": "List of books"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only lawyers may access books"},
    },
)
async def get_all_books(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: BookService = Depends(get_book_service),
) -> List[BookResponse]:
    return await service.get_all_books(
        current_user.user_id,
        limit,
        offset,
    )


# ── Create ──────────────────────────────────────────────────────────

@router.post(
    "/",
    response_model=BookResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a book",
    description="Create a new physical register book for the authenticated lawyer.",
    responses={
        201: {"description": "Book created"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only lawyers may create books"},
    },
)
async def create_book(
    request: Request,
    body: BookCreateRequest,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: BookService = Depends(get_book_service),
) -> BookResponse:
    return await service.create_book(current_user.user_id, body)


# ── Get one ─────────────────────────────────────────────────────────

@router.get(
    "/{book_id}",
    response_model=BookResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a book",
    description="Return a single book by ID. Only the owner may access it.",
    responses={
        200: {"description": "Book found"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only lawyers may access books"},
        404: {"description": "Book not found"},
    },
)
async def get_book(
    request: Request,
    book_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: BookService = Depends(get_book_service),
) -> BookResponse:
    return await service.get_book(current_user.user_id, book_id)


# ── Delete ──────────────────────────────────────────────────────────

@router.delete(
    "/{book_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a book",
    description="Delete a book and all its documents. Only the owner may delete.",
    responses={
        204: {"description": "Book deleted"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only lawyers may delete books"},
        404: {"description": "Book not found"},
    },
)
async def delete_book(
    request: Request,
    book_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: BookService = Depends(get_book_service),
) -> None:
    await service.delete_book(current_user.user_id, book_id)
