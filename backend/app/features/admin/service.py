import uuid

from app.features.admin.repository import InvitationRepository
from app.features.blockchain.repository import OnChainRecordRepository
from app.features.documents.repository import DocumentRepository
from app.features.users.repository import UserRepository
from app.infrastructure.email import (
    EmailMessage,
    EmailProvider,
    build_invitation_email,
    build_upgrade_email,
)
from app.infrastructure.observability.logging_setup import log
from app.shared.constants import ROLE_LAWYER
from app.shared.errors.exceptions import BadRequestError, NotFoundError
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from .schemas import (
    AdminDashboardResponse,
    AdminUserListResponse,
    AdminUserResponse,
    InvitationListResponse,
    InvitationResponse,
)


class AdminService:
    def __init__(self, session: AsyncSession, email_provider: EmailProvider) -> None:
        self.session = session
        self.email = email_provider
        self.inv_repo = InvitationRepository(session)
        self.user_repo = UserRepository(session)
        self.doc_repo = DocumentRepository(session)
        self.onchain_repo = OnChainRecordRepository(session)

    # === Dashboard ===
    async def get_dashboard(self) -> AdminDashboardResponse:
        total_users = await self.user_repo.count()
        total_lawyers = await self.user_repo.count_by_role("lawyer")
        total_docs = await self.doc_repo.count_active()
        total_processed = await self.doc_repo.count_by_status("COMPLETED")
        total_failed = await self.doc_repo.count_by_status("FAILED")
        total_on_chain = await self.onchain_repo.count()
        pending = len(await self.inv_repo.get_all_pending())
        return AdminDashboardResponse(
            total_users=total_users,
            total_lawyers=total_lawyers,
            total_documents=total_docs,
            total_processed=total_processed,
            total_failed=total_failed,
            total_on_chain=total_on_chain,
            pending_invitations=pending,
        )

    # === Users ===
    async def get_all_users(self) -> AdminUserListResponse:
        users = await self.user_repo.get_all()
        return AdminUserListResponse(
            users=[AdminUserResponse.model_validate(u) for u in users],
            total=len(users),
        )

    # Added 2026-09-12 16:59 PST — the write side of the is_active guard.
    # Two refusals, both about not being able to undo the action:
    #  - Self: only a lawyer can reach this endpoint, so deactivating yourself
    #    locks you out of the endpoint that would restore you.
    #  - Last active lawyer: same trap one step out. With no active lawyer left,
    #    nobody can reach /admin at all and the only way back is editing the
    #    database — which is exactly what this endpoint exists to avoid.
    # Deactivating does not revoke the Supabase session; it stops being accepted
    # at get_current_user on the user's very next request, which is the same
    # thing from the client's side.
    async def set_user_active(
        self, actor_id: uuid.UUID, user_id: uuid.UUID, is_active: bool
    ) -> AdminUserResponse:
        user = await self.user_repo.get_by_id(user_id)
        if not user:
            raise NotFoundError("User not found")

        if not is_active and user.id == actor_id:
            raise BadRequestError("You cannot deactivate your own account")

        if not is_active and user.is_active and user.role == ROLE_LAWYER:
            active_lawyers = await self.user_repo.count_active_by_role(ROLE_LAWYER)
            if active_lawyers <= 1:
                raise BadRequestError(
                    "Cannot deactivate the last active lawyer — "
                    "no one would be left who can restore access."
                )

        await self.user_repo.update(user, is_active=is_active)
        await self.session.commit()

        log.info(
            "admin.user.active.changed",
            actor_id=str(actor_id),
            user_id=str(user.id),
            is_active=is_active,
        )
        return AdminUserResponse.model_validate(user)

    # === Invitations ===
    async def create_invitation(
        self, email: str, role: str, request: Request
    ) -> InvitationResponse:
        existing = await self.inv_repo.get_by_email(email)
        if existing and existing.status == "pending":
            raise BadRequestError(
                "An active invitation already exists for this email"
            )

        # Added 2026-09-07 00:01 PST — reuse the existing row instead of
        # inserting a duplicate. Invitation.email is unique, but the guard above
        # only rejects *pending* rows, so re-inviting an address whose
        # invitation had already been claimed or expired fell straight through
        # to create() and raised an IntegrityError — a 500 on an ordinary
        # re-invite (someone leaves and is later brought back, an expired invite
        # is sent again). The stale row is reissued instead, which also
        # invalidates the previous token.
        if existing:
            invitation = await self.inv_repo.reissue(existing, role)
        else:
            invitation = await self.inv_repo.create(email=email, role=role)

        magic_link = f"lexchain://sign-up?token={invitation.token}"

        user = await self.user_repo.get_by_email(email)
        if user and user.role != role:
            # ── Existing user: upgrade role directly ──
            old_role = user.role
            user.role = role
            invitation.status = "claimed"
            invitation.claimed_by = user.id
            await self.session.commit()

            html_body, text_body = build_upgrade_email(role)
            await self.email.send(
                EmailMessage(
                    to=email,
                    subject="Your LexChain account has been upgraded",
                    html=html_body,
                    text=text_body,
                )
            )

            log.info(
                "admin.user.upgraded", email=email, old_role=old_role, new_role=role
            )
        else:
            # ── New user: send invitation email with magic link ──
            await self.session.commit()

            html_body, text_body = build_invitation_email(role, magic_link)
            await self.email.send(
                EmailMessage(
                    to=email,
                    subject="You've been invited to join LexChain",
                    html=html_body,
                    text=text_body,
                )
            )

            log.info("admin.invitation.created", email=email, role=role)

        return InvitationResponse(
            id=invitation.id,
            email=invitation.email,
            role=invitation.role,
            status=invitation.status,
            expires_at=invitation.expires_at,
            created_at=invitation.created_at,
            magic_link=magic_link if invitation.status == "pending" else None,
        )

    async def get_invitations(self) -> InvitationListResponse:
        all_inv = await self.inv_repo.get_all()
        return InvitationListResponse(
            invitations=[InvitationResponse.model_validate(i) for i in all_inv],
        )

    async def revoke_invitation(self, invitation_id: uuid.UUID) -> None:
        inv = await self.inv_repo.get_by_id(invitation_id)
        if not inv:
            raise NotFoundError("Invitation not found")
        inv.status = "expired"
        await self.session.commit()
        log.info("admin.invitation.revoked", invitation_id=str(invitation_id))
