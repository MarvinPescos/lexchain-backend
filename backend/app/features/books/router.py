import uuid
from typing import List

from fastapi import APIRouter, Depends, Query, Request, status

from app.infrastructure.security.dependencies import (
    AuthenticatedUser,
    require_lawyer,
)

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
    # Changed 2026-09-24 13:47 PST — bounded paging, same as /notifications.
    # A bare int let `limit=1000000` through to the query and a negative
    # offset reach Postgres, which rejects it with a 500 instead of a 422.
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
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
    description=(
        "Create a register book. OPEN for the book in current use — at most one "
        "per year. CLOSED to migrate a finished physical book: documents filed "
        "in it must give their Doc. No. and Page No. from the paper register."
    ),
    responses={
        201: {"description": "Book created"},
        400: {
            "description": "Book number already used for that year, or another book for that year is still open"
        },
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


# ── Close ───────────────────────────────────────────────────────────
# Added 2026-09-24 15:10 PST — the way to finish a register and open the next.
# Only one book per year can be OPEN, so the next book for a year can only be
# opened once the current one is closed (or fills its last page).


@router.post(
    "/{book_id}/close",
    response_model=BookResponse,
    status_code=status.HTTP_200_OK,
    summary="Close a book",
    description=(
        "Mark the register as finished so the next book for its year can be "
        "opened. One-way. Documents can still be filed in a closed book with "
        "explicit Doc. No. and Page No."
    ),
    responses={
        200: {"description": "Book closed"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only lawyers may close books"},
        404: {"description": "Book not found"},
        409: {"description": "Book is already closed"},
    },
)
async def close_book(
    request: Request,
    book_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: BookService = Depends(get_book_service),
) -> BookResponse:
    return await service.close_book(current_user.user_id, book_id)


# ── Delete ──────────────────────────────────────────────────────────


@router.delete(
    "/{book_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a book",
    description=(
        "Delete an empty book. Only the owner may delete, and only while no "
        "document is filed in it — documents, finalized ones especially, are "
        "never removed by deleting their book."
    ),
    responses={
        204: {"description": "Book deleted"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Forbidden — only lawyers may delete books"},
        404: {"description": "Book not found"},
        409: {"description": "Book still holds documents"},
    },
)
async def delete_book(
    request: Request,
    book_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_lawyer),
    service: BookService = Depends(get_book_service),
) -> None:
    await service.delete_book(current_user.user_id, book_id)
