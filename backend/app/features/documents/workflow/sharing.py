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
    PARTY_STATUS_EXPIRED,
    PARTY_STATUS_PENDING,
    PRIVILEGED_PARTY_ROLES,
    ROLE_LAWYER,
)
from app.shared.errors.exceptions import BadRequestError, ConflictError, NotFoundError
from sqlalchemy.ext.asyncio import AsyncSession

from ..access import DocumentAccess
from ..constants import ACTION_SHARE, PARTY_INVITE_TTL
from ..models import DocumentParty, DraftRevocation
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
    from app.features.users.models import User

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
            return await self._invite_by_email(doc, owner_id, email, role)

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
        assert owner is not None

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
                    invited_by_name=_name(r.document.user if r.document else None),
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
            await self._grant_draft_access(invitation)

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
        assert owner is not None

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
            parties=[party_response(p) for p in parties],
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
            # An invitation to someone with no account has no user id, so it is
            # removed by its party record id.
            invite = await self.repo.get_email_invite_by_id(document_id, party_user_id)
            if invite is None:
                raise NotFoundError("Party not found on this document")
            await self.repo.delete(invite)
            await self.session.commit()
            log.info(
                "document.party.invite_removed",
                document_id=str(document_id),
                owner_id=str(user_id),
                party_id=str(party_user_id),
            )
            return RemovePartyResponse(
                document_id=document_id,
                user_id=party_user_id,
                message="Invitation removed",
            )

        # LexChain access ends now. The Drive permission is recorded in the
        # same commit, so a failed revocation is retried rather than forgotten.
        document = await self.repo.get_by_id(document_id)
        assert document is not None
        revocation = None
        if document.draft_file_id and party.draft_permission_id:
            revocation = DraftRevocation(
                document_id=document_id,
                draft_file_id=document.draft_file_id,
                permission_id=party.draft_permission_id,
            )
            await self.repo.add(revocation)
        # A declined invitation was never seen as access, so there is nothing
        # to tell them.
        notify = party.status != PARTY_STATUS_DECLINED
        was_pending = party.status == PARTY_STATUS_PENDING
        await self.repo.remove_party(document_id, party_user_id)
        await self.session.commit()

        if revocation is not None:
            await self.drafting.revoke(revocation)
        log.info(
            "document.party.remove",
            document_id=str(document_id),
            owner_id=str(user_id),
            party_id=str(party_user_id),
        )

        if notify:
            from app.celery_tasks import handle_party_removed
            from app.features.users.repository import UserRepository

            owner = await UserRepository(self.session).get_by_id(document.user_id)
            assert owner is not None
            handle_party_removed.delay(
                party_user_id=str(party_user_id),
                owner_name=f"{owner.f_name} {owner.l_name}",
                document_title=document.file_name,
                document_id=str(document_id),
                was_pending=was_pending,
            )

        return RemovePartyResponse(
            document_id=document_id,
            user_id=party_user_id,
            message="Party removed",
        )

    async def claim_email_invites(self, user_id: uuid.UUID, email: str) -> int:
        """Add a newly verified account to every document its address was invited to.

        Called at sign-in, once Supabase has confirmed the address belongs to
        this person; signing up alone proves nothing about the email.
        """
        parties = await self.repo.claim_email_invites(user_id, email)
        if not parties:
            return 0
        await self.session.commit()
        for party in parties:
            await self._grant_draft_access(party)
            log.info(
                "document.invitation.claimed",
                user_id=str(user_id),
                document_id=str(party.document_id),
            )
        return len(parties)

    async def resend_invite(
        self, user: AuthenticatedUser, document_id: uuid.UUID, party_id: uuid.UUID
    ) -> DocumentParty:
        """Email an invitation again and give it a fresh expiry. Owner (or admin) only.

        Only for invitations to an address with no account: anyone with an
        account already sees theirs in the app.
        """
        doc = await self.access.authorize(user, document_id, ACTION_SHARE)
        party = await self.repo.get_email_invite_by_id(document_id, party_id)
        if party is None:
            raise NotFoundError("No email invitation with that id on this document")

        await self._renew(party)
        await self.session.commit()
        log.info(
            "document.party.invite_resent",
            document_id=str(document_id),
            owner_id=str(user.user_id),
            party_id=str(party_id),
        )
        await self._email_invite(doc, party)
        return party

    # ========== helpers =============

    async def _invite_by_email(
        self, doc, owner_id: uuid.UUID, email: str, role: str
    ) -> DocumentParty:
        """Invite an address with no account. It joins when they first sign in."""
        if role in PRIVILEGED_PARTY_ROLES:
            raise BadRequestError(
                f"Cannot add as {role}. Only lawyers can be signers or editors, "
                "and this email has no LexChain account yet."
            )
        party = await self.repo.get_email_invite(doc.id, email)
        if party is not None and not is_expired(party):
            raise ConflictError("This email already has a pending invitation")

        if party is None:
            party = await self.repo.add_party(doc.id, None, role, email.lower())
            await self.session.refresh(party, ["user"])
        else:
            # Inviting an expired address again reopens its invitation.
            party.role = role
        await self._renew(party)
        await self.session.commit()

        log.info(
            "document.party.invited_by_email",
            document_id=str(doc.id),
            owner_id=str(owner_id),
            role=role,
        )
        await self._email_invite(doc, party)
        return party

    async def _renew(self, party: DocumentParty) -> None:
        party.invite_expires_at = datetime.now(timezone.utc) + PARTY_INVITE_TTL
        await self.session.flush()

    async def _email_invite(self, doc, party: DocumentParty) -> None:
        from app.celery_tasks import send_document_invite_email
        from app.features.users.repository import UserRepository

        owner = await UserRepository(self.session).get_by_id(doc.user_id)
        assert owner is not None
        send_document_invite_email.delay(
            email=party.email,
            owner_name=f"{owner.f_name} {owner.l_name}",
            document_name=doc.file_name,
        )

    async def _grant_draft_access(self, party: DocumentParty) -> None:
        """Mirror an accepted party onto the working draft in Google Docs.

        Never blocks the LexChain side: if Drive is unreachable or the lawyer
        disconnected Google, the party's LexChain access still changes and the
        draft is brought in line the next time it is locked or unlocked.
        """
        document_id, party_id = party.document_id, party.id
        document = await self.repo.get_by_id(document_id)
        assert document is not None
        try:
            await self.drafting.grant_party(document, party)
            await self.session.commit()
        except Exception as e:
            # Nothing to undo: the permission id is only written once Drive
            # has answered.
            log.error(
                "document.draft.access_sync_failed",
                document_id=str(document_id),
                party_id=str(party_id),
                error=f"{type(e).__name__}: {e}",
            )


def is_expired(party: DocumentParty) -> bool:
    """An email invitation past its expiry. Parties with an account never expire."""
    return (
        party.user_id is None
        and party.invite_expires_at is not None
        and party.invite_expires_at <= datetime.now(timezone.utc)
    )


def party_response(party: DocumentParty) -> DocumentPartyResponse:
    """One party as the API shows it; `user` must be loaded."""
    user = party.user
    return DocumentPartyResponse(
        id=party.id,
        user_id=party.user_id,
        # An email invitation has no account behind it yet.
        email=user.email if user else party.email,
        f_name=user.f_name if user else "",
        l_name=user.l_name if user else "",
        role=party.role,
        status=PARTY_STATUS_EXPIRED if is_expired(party) else party.status,
        responded_at=party.responded_at,
        expires_at=party.invite_expires_at if party.user_id is None else None,
        created_at=party.created_at,
    )


def _name(user: "User | None") -> str:
    return f"{user.f_name} {user.l_name}".strip() if user else ""
