"""Who may do what to a document — the one place that decides.

    view                     owner, or a party whose invitation was accepted
    every other action       owner only (a lawyer), and only if `lifecycle`
                             allows it from the document's current stage
    the signed original      owner; a party only once their e-copy request
                             was approved for the document's current file —
                             otherwise a watermarked preview

There is no super-user tier: admin and lawyer are one role, so no account can
reach another lawyer's documents. Client confidentiality depends on it.
Unknown and forbidden both answer 404 so document ids cannot be probed; a
party attempting an owner action gets 403, since they can already see it.
"""

import uuid

from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.errors.exceptions import ForbiddenError, NotFoundError
from sqlalchemy.ext.asyncio import AsyncSession

from . import lifecycle
from .constants import (
    ACTION_ATTACH_SIGNED_COPY,
    ACTION_CANCEL,
    ACTION_CORRECT_ENTRY,
    ACTION_FINALIZE,
    ACTION_CREATE_DRAFT,
    ACTION_MARK_READY,
    ACTION_RENAME,
    ACTION_REOPEN,
    ACTION_REPLACE_SIGNED_COPY,
    ACTION_RESTORE,
    ACTION_VIEW,
    LIFECYCLE_FINALIZED,
    STATUS_COMPLETED,
    STATUS_FAILED,
)
from .models import Document, SignedCopy
from .repository import DocumentRepository
from .schemas import DocumentPermissions


class DocumentAccess:
    def __init__(self, session: AsyncSession) -> None:
        self.repo = DocumentRepository(session)

    async def authorize(
        self, user: AuthenticatedUser, document_id: uuid.UUID, action: str = ACTION_VIEW
    ) -> Document:
        """Return the document if `user` may perform `action` on it now, else raise."""
        document = await self.repo.get_one(user.user_id, document_id)
        if document is None:
            document = await self.repo.get_by_id(document_id)
            if document is None or not await self.repo.is_party(
                user.user_id, document_id
            ):
                raise NotFoundError("Document not found")
            if action != ACTION_VIEW:
                raise ForbiddenError("Only the document owner may perform this action")

        if action in lifecycle.TRANSITIONS:
            lifecycle.ensure(action, document)
        return document

    async def may_read_original(
        self, user: AuthenticatedUser, document: Document, copy: SignedCopy
    ) -> bool:
        """Whether `user` may have the unwatermarked bytes of `copy`.

        Call after `authorize`. An approval covers the bytes it was given
        for, and only while they are still the document's current file.
        """
        if document.user_id == user.user_id:
            return True
        if document.document_hash is None or copy.sha256 != document.document_hash:
            return False
        return await self.repo.has_approved_ecopy(
            document.id, user.user_id, document.document_hash
        )

    def permissions(
        self, user: AuthenticatedUser, document: Document
    ) -> DocumentPermissions:
        """The action matrix for this user on this document, mirroring `authorize`."""
        is_owner = document.user_id == user.user_id

        def allowed(action: str) -> bool:
            return is_owner and lifecycle.can(action, document)

        return DocumentPermissions(
            can_view=True,
            can_rename=allowed(ACTION_RENAME),
            can_create_draft=allowed(ACTION_CREATE_DRAFT)
            and document.draft_file_id is None,
            can_mark_ready=allowed(ACTION_MARK_READY),
            can_reopen=allowed(ACTION_REOPEN),
            can_attach_signed_copy=allowed(ACTION_ATTACH_SIGNED_COPY),
            can_replace_signed_copy=allowed(ACTION_REPLACE_SIGNED_COPY),
            can_correct_entry=allowed(ACTION_CORRECT_ENTRY),
            can_cancel=allowed(ACTION_CANCEL),
            # Mirrors finalization.ensure_processed.
            can_finalize=allowed(ACTION_FINALIZE)
            and document.status == STATUS_COMPLETED,
            can_retry_processing=is_owner and document.status == STATUS_FAILED,
            can_share=is_owner,
            can_revoke=is_owner,
            can_request_ecopy=not is_owner
            and document.lifecycle == LIFECYCLE_FINALIZED
            and document.signed_copy_id is not None,
            # Not by stage: `lifecycle` may be the very thing that was edited.
            can_restore=allowed(ACTION_RESTORE) and document.on_chain,
            can_review_ecopy=is_owner,
        )
