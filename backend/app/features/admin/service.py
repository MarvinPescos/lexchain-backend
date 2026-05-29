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
        total_docs = await self.doc_repo.count_latest()
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

    # === Invitations ===
    async def create_invitation(
        self, email: str, role: str, request: Request
    ) -> InvitationResponse:
        existing = await self.inv_repo.get_by_email(email)
        if existing and existing.status == "pending":
            raise BadRequestError(
                "An active invitation already exists for this email", 400
            )

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
