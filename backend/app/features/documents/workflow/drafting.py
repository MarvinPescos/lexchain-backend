"""The preparation stage of a Path 1 document.

The working draft is a Google Doc in the lawyer's own Drive. LexChain creates
it, shares it with the document's clients, locks it when the text is agreed
and unlocks it if the document goes back to preparing. It never reads the
draft's content: the signed PDF attached later is the legal document.
"""

import uuid
from datetime import datetime, timezone
from pathlib import PurePath

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.google.service import GoogleConnectionService
from app.infrastructure.drafts import (
    ROLE_COMMENTER,
    ROLE_READER,
    ROLE_WRITER,
    DraftWorkspace,
)
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.constants import PARTY_ROLE_EDITOR, PARTY_STATUS_ACCEPTED
from app.shared.errors.exceptions import BadRequestError, ConflictError

from .. import lifecycle
from ..access import DocumentAccess
from ..constants import (
    ACTION_CANCEL,
    ACTION_CREATE_DRAFT,
    ACTION_MARK_READY,
    ACTION_REOPEN,
    DRAFT_SOURCE_BLANK,
    DRAFT_SOURCE_EXISTING,
    DRAFT_SOURCE_TEMPLATE,
    DRAFT_SOURCE_UPLOAD,
    DRAFT_UPLOAD_TYPES,
    LIFECYCLE_PREPARING,
    LIFECYCLE_READY_FOR_SIGNATURE,
    MAX_DRAFT_UPLOAD_SIZE,
)
from ..models import Document, DocumentParty, DraftRevocation
from ..presenters import document_response
from ..repository import DocumentRepository
from ..schemas import DocumentResponse
from .discussion import DocumentDiscussion


def draft_role(party: DocumentParty, locked: bool) -> str:
    """The Drive role a party holds on the draft.

    Clients comment; another lawyer on the document (editor) edits. Once the
    document is ready for signature everyone but the owner can only read.
    """
    if locked:
        return ROLE_READER
    return ROLE_WRITER if party.role == PARTY_ROLE_EDITOR else ROLE_COMMENTER


class DocumentDrafting:
    def __init__(
        self,
        session: AsyncSession,
        workspace: DraftWorkspace,
        google: GoogleConnectionService,
    ) -> None:
        self.session = session
        self.workspace = workspace
        self.google = google
        self.repo = DocumentRepository(session)
        self.access = DocumentAccess(session)
        self.discussion = DocumentDiscussion(session, workspace, google)

    # === The draft ===

    async def create_draft(
        self,
        user: AuthenticatedUser,
        document_id: uuid.UUID,
        source: str = DRAFT_SOURCE_BLANK,
        file_id: str | None = None,
        file: UploadFile | None = None,
    ) -> DocumentResponse:
        """Create the working draft and share it with the document's clients.

        `blank` makes an empty Doc, `template` copies `file_id`, `existing`
        uses `file_id` itself, `upload` converts `file` (say, a Word file the
        client handed over) to a Doc. `file_id` comes from the Google Picker,
        which is also what grants LexChain access to that file.
        """
        document = await self.access.authorize(user, document_id, ACTION_CREATE_DRAFT)
        if document.draft_file_id:
            raise ConflictError("This document already has a working draft.")
        if source in (DRAFT_SOURCE_TEMPLATE, DRAFT_SOURCE_EXISTING) and not file_id:
            raise BadRequestError("Pick the Google Doc to use for the draft.")
        if source == DRAFT_SOURCE_UPLOAD and file is None:
            raise BadRequestError("Choose the file to use for the draft.")
        if source != DRAFT_SOURCE_UPLOAD and file is not None:
            raise BadRequestError("A file is accepted only with source 'upload'.")
        if file is not None:
            content, content_type = await self._read_upload(file)

        token = await self.google.access_token(document.user_id)
        if source == DRAFT_SOURCE_TEMPLATE:
            assert file_id is not None
            ref = await self.workspace.copy(token, file_id, document.file_name)
        elif source == DRAFT_SOURCE_EXISTING:
            assert file_id is not None
            ref = await self.workspace.get(token, file_id)
        elif source == DRAFT_SOURCE_UPLOAD:
            ref = await self.workspace.upload(
                token, document.file_name, content, content_type
            )
        else:
            ref = await self.workspace.create(token, document.file_name)

        document.draft_file_id = ref.file_id
        document.draft_url = ref.url
        if source != DRAFT_SOURCE_EXISTING:
            # A Doc LexChain just made has no history: every comment from here
            # on is new and must notify. Only an existing Doc gets the silent
            # first import (see DocumentDiscussion.sync).
            document.comments_synced_at = datetime.now(timezone.utc)
        change = lifecycle.transition(ACTION_CREATE_DRAFT, document, user.user_id)
        await self.repo.add(change)
        # Commit before sharing: the Doc exists in Drive now and a sharing
        # failure must not lose track of it.
        await self.session.commit()

        for party in await self._accepted_parties(document.id):
            await self._grant(token, document, party, locked=False)
        await self.session.commit()

        log.info(
            "document.draft.create",
            document_id=str(document_id),
            source=source,
            file_id=ref.file_id,
        )
        return await document_response(self.session, user, document)

    # === Stage moves ===

    async def mark_ready(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> DocumentResponse:
        """The text is agreed: lock the draft for printing, then move the stage.

        Refused while any comment on the draft is open — every question is
        closed before the text is frozen.
        """
        document = await self.access.authorize(user, document_id, ACTION_MARK_READY)
        if document.draft_file_id:
            unresolved = await self.discussion.sync(document)
            # Keep what the sync found (and notified) even if Ready is refused.
            await self.session.commit()
            self.discussion.send_emails()
            if unresolved:
                raise ConflictError(
                    f"{unresolved} comment(s) on the draft are still open. "
                    "Resolve them in Google Docs before marking it ready.",
                    details={"reason": "UNRESOLVED_COMMENTS", "unresolved": unresolved},
                )
        # Lock first: if Drive refuses, the document stays PREPARING rather
        # than claiming a frozen text clients can still change.
        await self._set_locked(document, locked=True)
        return await self._transition(user, document, ACTION_MARK_READY)

    async def reopen(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> DocumentResponse:
        """Back to preparing — the client asked for a change before signing."""
        document = await self.access.authorize(user, document_id, ACTION_REOPEN)
        await self._set_locked(document, locked=False)
        return await self._transition(user, document, ACTION_REOPEN)

    async def cancel(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> DocumentResponse:
        """Abandon a document that was never signed. Kept for the record."""
        document = await self.access.authorize(user, document_id, ACTION_CANCEL)
        change = lifecycle.transition(ACTION_CANCEL, document, user.user_id)
        document.cancelled_at = datetime.now(timezone.utc)
        await self.repo.add(change)
        await self.session.commit()
        log.info("document.cancel", document_id=str(document_id))
        return await document_response(self.session, user, document)

    # === Called by sharing ===

    async def grant_party(self, document: Document, party: DocumentParty) -> None:
        """Give a newly accepted party access to the draft, if there is one."""
        if not document.draft_file_id or document.lifecycle not in (
            LIFECYCLE_PREPARING,
            LIFECYCLE_READY_FOR_SIGNATURE,
        ):
            return
        token = await self.google.access_token(document.user_id)
        await self._grant(
            token,
            document,
            party,
            locked=document.lifecycle == LIFECYCLE_READY_FOR_SIGNATURE,
        )

    async def revoke(self, revocation: DraftRevocation) -> bool:
        """Take a removed party's permission off the draft; True once it is gone.

        The revocation row is deleted only after Drive confirms (an already
        missing permission counts). On failure it stays, with the error, for
        the retry task.
        """
        try:
            document = await self.repo.get_by_id(revocation.document_id)
            token = await self.google.access_token(document.user_id)
            await self.workspace.unshare(
                token, revocation.draft_file_id, revocation.permission_id
            )
        except Exception as e:
            revocation.attempts += 1
            revocation.last_error = f"{type(e).__name__}: {e}"
            await self.session.commit()
            log.error(
                "document.draft.revoke_failed",
                document_id=str(revocation.document_id),
                permission_id=revocation.permission_id,
                attempts=revocation.attempts,
                error=revocation.last_error,
            )
            return False

        await self.session.delete(revocation)
        await self.session.commit()
        log.info(
            "document.draft.revoked",
            document_id=str(revocation.document_id),
            permission_id=revocation.permission_id,
        )
        return True

    # ========== helpers =============

    async def _transition(
        self, user: AuthenticatedUser, document: Document, action: str
    ) -> DocumentResponse:
        change = lifecycle.transition(action, document, user.user_id)
        await self.repo.add(change)
        await self.session.commit()
        log.info(
            f"document.{action}",
            document_id=str(document.id),
            lifecycle=document.lifecycle,
        )
        return await document_response(self.session, user, document)

    async def _set_locked(self, document: Document, locked: bool) -> None:
        if not document.draft_file_id:
            return
        token = await self.google.access_token(document.user_id)
        for party in await self._accepted_parties(document.id):
            if party.draft_permission_id:
                await self.workspace.set_role(
                    token,
                    document.draft_file_id,
                    party.draft_permission_id,
                    draft_role(party, locked),
                )
            else:
                # Accepted while Google was unreachable: share now.
                await self._grant(token, document, party, locked)

    async def _grant(
        self, token: str, document: Document, party: DocumentParty, locked: bool
    ) -> None:
        party.draft_permission_id = await self.workspace.share(
            token, document.draft_file_id, party.email, draft_role(party, locked)
        )
        # Drive reuses a person's permission id on a file. If they were removed
        # and are back, a pending revocation would take the new access away.
        await self.repo.drop_draft_revocation(
            document.draft_file_id, party.draft_permission_id
        )
        await self.session.flush()

    async def _read_upload(self, file: UploadFile) -> tuple[bytes, str]:
        """The uploaded file's bytes and the MIME type Drive converts from.

        The extension and the declared type must agree: a renamed image is
        not a Word file.
        """
        accepted = DRAFT_UPLOAD_TYPES.get(PurePath(file.filename or "").suffix.lower())
        if not accepted or file.content_type not in accepted:
            allowed = ", ".join(ext.lstrip(".") for ext in DRAFT_UPLOAD_TYPES)
            raise BadRequestError(f"Unsupported file type. Allowed: {allowed}")
        # One byte past the limit is enough to know it is too big; never buffer more.
        too_big = BadRequestError(
            f"File exceeds the maximum allowed size of "
            f"{MAX_DRAFT_UPLOAD_SIZE // (1024 * 1024)} MB"
        )
        if file.size is not None and file.size > MAX_DRAFT_UPLOAD_SIZE:
            raise too_big
        content = await file.read(MAX_DRAFT_UPLOAD_SIZE + 1)
        if len(content) > MAX_DRAFT_UPLOAD_SIZE:
            raise too_big
        if not content:
            raise BadRequestError("File is empty")
        return content, accepted[0]

    async def _accepted_parties(self, document_id: uuid.UUID) -> list[DocumentParty]:
        return [
            p
            for p in await self.repo.get_parties(document_id)
            if p.status == PARTY_STATUS_ACCEPTED
        ]
