"""Who else can see a document: parties and their invitations.

An invitation is not access. Until the invitee accepts, the party row grants
nothing and the document does not appear in their list.
"""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.infrastructure.observability.logging_setup import log
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.constants import (
    PARTY_ROLES,
    PARTY_STATUS_ACCEPTED,
    PARTY_STATUS_DECLINED,
    PARTY_STATUS_PENDING,
    PRIVILEGED_PARTY_ROLES,
    ROLE_LAWYER,
)
from app.shared.errors.exceptions import BadRequestError, ConflictError, NotFoundError
from sqlalchemy.ext.asyncio import AsyncSession

from ..access import DocumentAccess
from ..constants import ACTION_SHARE
from ..models import DocumentParty
from ..repository import DocumentRepository
from ..schemas import (
    DocumentInvitationActionResponse,
    DocumentInvitationListResponse,
    DocumentInvitationResponse,
    DocumentPartyListResponse,
    DocumentPartyResponse,
    RemovePartyResponse,
)

if TYPE_CHECKING:
    from .drafting import DocumentDrafting


class DocumentSharing:
    def __init__(self, session: AsyncSession, drafting: "DocumentDrafting") -> None:
        self.session = session
        self.drafting = drafting
        self.repo = DocumentRepository(session)
        self.access = DocumentAccess(session)

    async def add_party(
        self,
        user: AuthenticatedUser,
        document_id: uuid.UUID,
        email: str,
        role: str = "viewer",
    ):
        """Whitelist a user on this document. Owner (or admin) only."""
        owner_id = user.user_id
        doc = await self.access.authorize(user, document_id, ACTION_SHARE)

        if role not in PARTY_ROLES:
            raise BadRequestError(
                f"Invalid role '{role}'. Allowed: {', '.join(PARTY_ROLES)}"
            )

        from app.features.users.repository import UserRepository

        user_repo = UserRepository(self.session)
        party_user = await user_repo.get_by_email(email)
        if not party_user:
            raise NotFoundError("User with that email not found")

        if party_user.id == doc.user_id:
            raise BadRequestError("The document owner is already a party")

        if role in PRIVILEGED_PARTY_ROLES and party_user.role != ROLE_LAWYER:
            raise BadRequestError(
                f"Cannot add as {role}. Only lawyers can be signers or editors."
            )

        # The unique (document_id, user_id) constraint means a re-invite has to
        # be handled rather than inserted twice.
        existing = await self.repo.get_party(document_id, party_user.id)
        if existing:
            if existing.status == PARTY_STATUS_PENDING:
                raise ConflictError("This user already has a pending invitation")
            if existing.status == PARTY_STATUS_ACCEPTED:
                raise ConflictError("This user already has access to the document")
            # Previously declined — re-inviting reopens it.
            party = await self.repo.update(
                existing,
                role=role,
                status=PARTY_STATUS_PENDING,
                responded_at=None,
            )
        else:
            party = await self.repo.add_party(document_id, party_user.id, role, email)

        await self.session.refresh(party, ["user"])
        await self.session.commit()

        log.info(
            "document.party.add",
            document_id=str(document_id),
            owner_id=str(owner_id),
            email=email,
            role=role,
        )

        owner = await user_repo.get_by_id(doc.user_id)

        from app.celery_tasks import handle_party_added

        handle_party_added.delay(
            party_user_id=str(party_user.id),
            owner_name=f"{owner.f_name} {owner.l_name}",
            document_title=doc.file_name,
            document_id=str(document_id),
        )

        return party

    # === Invitations ===

    async def list_invitations(
        self, user: AuthenticatedUser
    ) -> DocumentInvitationListResponse:
        """Invitations awaiting this user's response.

        Separate from the document list on purpose: a pending invitation grants
        no access, so the document must not appear among the user's documents
        until they accept.
        """
        rows = await self.repo.get_pending_invitations(user.user_id)

        log.info(
            "document.invitations.list",
            user_id=str(user.user_id),
            pending=len(rows),
        )

        return DocumentInvitationListResponse(
            invitations=[
                DocumentInvitationResponse(
                    invitation_id=r.id,
                    document_id=r.document_id,
                    file_name=r.document.file_name if r.document else "",
                    role=r.role,
                    status=r.status,
                    invited_at=r.created_at,
                    responded_at=r.responded_at,
                )
                for r in rows
            ],
            total=len(rows),
        )

    async def respond_to_invitation(
        self, user: AuthenticatedUser, invitation_id: uuid.UUID, accept: bool
    ) -> DocumentInvitationActionResponse:
        """Accept or decline an invitation. Only the invitee may respond."""
        invitation = await self.repo.get_invitation(invitation_id, user.user_id)
        if not invitation:
            # Scoped to the invitee, so someone else's invitation reads as absent.
            raise NotFoundError("Invitation not found")

        if invitation.status != PARTY_STATUS_PENDING:
            raise ConflictError(f"This invitation was already {invitation.status}")

        status = PARTY_STATUS_ACCEPTED if accept else PARTY_STATUS_DECLINED
        await self.repo.update(
            invitation,
            status=status,
            responded_at=datetime.now(timezone.utc),
        )
        await self.session.commit()

        if accept:
            await self._sync_draft_access(invitation, grant=True)

        log.info(
            "document.invitation.responded",
            user_id=str(user.user_id),
            invitation_id=str(invitation_id),
            document_id=str(invitation.document_id),
            status=status,
        )

        return DocumentInvitationActionResponse(
            invitation_id=invitation_id,
            document_id=invitation.document_id,
            status=status,
            message=(
                "Invitation accepted. The document is now in your list."
                if accept
                else "Invitation declined. You will not have access to this document."
            ),
        )

    async def get_parties(self, user: AuthenticatedUser, document_id: uuid.UUID):
        """List everyone with access. Visible to the owner, any party, or admin."""
        doc = await self.access.authorize(user, document_id)

        from app.features.users.repository import UserRepository

        user_repo = UserRepository(self.session)
        owner = await user_repo.get_by_id(doc.user_id)

        issuer = DocumentPartyResponse(
            id=owner.id,
            user_id=owner.id,
            email=owner.email,
            f_name=owner.f_name,
            l_name=owner.l_name,
            role="owner",
            # status=owner.status,
            created_at=doc.created_at,
        )

        parties = await self.repo.get_parties(document_id)

        return DocumentPartyListResponse(
            document_id=document_id,
            issuer=issuer,
            parties=[
                DocumentPartyResponse(
                    id=p.id,
                    user_id=p.user_id,
                    email=p.user.email,
                    f_name=p.user.f_name,
                    l_name=p.user.l_name,
                    role=p.role,
                    status=p.status,
                    responded_at=p.responded_at,
                    created_at=p.created_at,
                )
                for p in parties
            ],
        )

    async def remove_party(
        self,
        user: AuthenticatedUser,
        document_id: uuid.UUID,
        party_user_id: uuid.UUID,
    ):
        """Revoke a user's access. Owner (or admin) only."""
        user_id = user.user_id
        await self.access.authorize(user, document_id, ACTION_SHARE)

        party = await self.repo.get_party(document_id, party_user_id)
        if party is None:
            raise NotFoundError("Party not found on this document")
        await self._sync_draft_access(party, grant=False)

        await self.repo.remove_party(document_id, party_user_id)
        await self.session.commit()
        log.info(
            "document.party.remove",
            document_id=str(document_id),
            owner_id=str(user_id),
            party_id=str(party_user_id),
        )

        return RemovePartyResponse(
            document_id=document_id,
            user_id=party_user_id,
            message="Party removed",
        )

    # ========== helpers =============

    async def _sync_draft_access(self, party: DocumentParty, grant: bool) -> None:
        """Mirror a party change onto the working draft in Google Docs.

        Never blocks the LexChain side: if Drive is unreachable or the lawyer
        disconnected Google, the party's LexChain access still changes and the
        draft is brought in line the next time it is locked or unlocked.
        """
        document_id, party_id = party.document_id, party.id
        document = await self.repo.get_by_id(document_id)
        try:
            if grant:
                await self.drafting.grant_party(document, party)
            else:
                await self.drafting.revoke_party(document, party)
            await self.session.commit()
        except Exception as e:
            # Nothing to undo: the permission id is only written once Drive
            # has answered.
            log.error(
                "document.draft.access_sync_failed",
                document_id=str(document_id),
                party_id=str(party_id),
                grant=grant,
                error=f"{type(e).__name__}: {e}",
            )
