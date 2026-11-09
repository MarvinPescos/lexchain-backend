import uuid

from app.infrastructure.security.dependencies import AuthenticatedUser, require_auth
from fastapi import APIRouter, Depends, status

from .dependencies import get_rag_service, get_retrieval_service
from .schemas import (
    AskRequest,
    GlobalSearchResponse,
    SearchRequest,
    SearchResponse,
)
from .service import RAGService, RetrievalService

router = APIRouter(tags=["Retrieval"])


@router.post(
    "/documents/{document_id}/search",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Semantic search within a single document",
)
async def search_document(
    document_id: uuid.UUID,
    body: SearchRequest,
    current_user: AuthenticatedUser = Depends(require_auth),
    service: RetrievalService = Depends(get_retrieval_service),
) -> SearchResponse:
    return await service.search_document(
        user_id=current_user.user_id,
        document_id=document_id,
        query=body.query,
    )


@router.post("/documents/{document_id}/ask")
async def ask_document(
    document_id: uuid.UUID,
    body: AskRequest,
    current_user: AuthenticatedUser = Depends(require_auth),
    service: RAGService = Depends(get_rag_service),
):
    return await service.ask_document(
        user_id=current_user.user_id,
        document_id=document_id,
        question=body.question,
        history=body.history,
    )


@router.post(
    "/search",
    response_model=GlobalSearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Semantic search across the documents the user can read",
)
async def search_all(
    body: SearchRequest,
    current_user: AuthenticatedUser = Depends(require_auth),
    service: RetrievalService = Depends(get_retrieval_service),
) -> GlobalSearchResponse:
    return await service.search_all(
        user_id=current_user.user_id,
        query=body.query,
    )


# @router.post("/ask")
# async def ask_all(
#     body: AskRequest,
#     current_user: AuthenticatedUser = Depends(require_auth),
#     service: RAGService = Depends(get_rag_service),
# ):
#     return await service.ask_all(
#         user_id=current_user.user_id,
#         question=body.question,
#     )
