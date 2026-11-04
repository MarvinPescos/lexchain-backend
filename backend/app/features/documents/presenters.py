"""How a document is shown in API responses, as a given user may act on it.

Shared by every workflow that answers with a document, so the detail view is
built in one place.
"""

from app.features.insights import DocumentInsightRepository
from app.infrastructure.security.dependencies import AuthenticatedUser
from sqlalchemy.ext.asyncio import AsyncSession

from .access import DocumentAccess
from .models import Document, SignedCopy
from .schemas import DocumentResponse, SignedCopyResponse


def copy_response(copy: SignedCopy, document: Document) -> SignedCopyResponse:
    response = SignedCopyResponse.model_validate(copy)
    response.is_current = copy.id == document.signed_copy_id
    return response


async def document_response(
    session: AsyncSession, user: AuthenticatedUser, document: Document
) -> DocumentResponse:
    """The detail view of a document, as `user` may act on it."""
    # updated_at is server-generated on update; load it and the current copy.
    await session.refresh(document)
    await session.refresh(document, ["signed_copy"])
    insight = await DocumentInsightRepository(session).get_by_document_id(document.id)
    return DocumentResponse(
        document_id=document.id,
        file_name=document.file_name,
        lifecycle=document.lifecycle,
        status=document.status,
        on_chain=document.on_chain,
        draft_url=document.draft_url,
        signed_copy=copy_response(document.signed_copy, document)
        if document.signed_copy
        else None,
        document_hash=document.document_hash,
        book_id=document.book_id,
        doc_no=document.doc_no,
        page_no=document.page_no,
        finalized_at=document.finalized_at,
        finalized_by=document.finalized_by,
        cancelled_at=document.cancelled_at,
        permissions=DocumentAccess(session).permissions(user, document),
        summary=insight.summary if insight else None,
        labels=(insight.labels or {}).get("items", []) if insight else [],
        entities=(insight.entities or {}).get("items", []) if insight else [],
        risk_flags=(insight.risk_flags or {}).get("items", []) if insight else [],
        analysis_status=(insight.meta or {}).get("analysis_status") if insight else None,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )
