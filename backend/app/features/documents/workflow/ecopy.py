"""E-copy requests: a party asks for the unwatermarked signed copy, the owner decides.

Until a request is approved the party reads only a watermarked preview (see
`queries.get_signed_copy_file`). Approval binds to the document's current
file and emails it, as an attachment, to the client's verified account
address. The decision is committed before the email is attempted, and the
outcome of every send is recorded, so an approval whose email failed stays
visibly owed and the owner can retry it. Access is checked again at every
step: a client removed from the document is neither approved nor sent to.
"""

import asyncio
import hashlib
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.notifications.models import NotificationType
from app.features.notifications.service import NotificationService
from app.features.users.models import User
from app.features.users.repository import UserRepository
from app.infrastructure.clients.supabase_storage import get_storage_client
from app.infrastructure.email import (
    EmailAttachment,
    EmailMessage,
    EmailProvider,
    build_ecopy_email,
)
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.errors.exceptions import BadRequestError, ConflictError, NotFoundError

from ..access import DocumentAccess
from ..constants import (
    ACTION_REVIEW_ECOPY,
    DELIVERY_ERROR_ACCESS,
    DELIVERY_ERROR_EMAIL_UNVERIFIED,
    DELIVERY_ERROR_INTEGRITY,
    DELIVERY_ERROR_PROVIDER,
    DELIVERY_ERROR_STORAGE,
    DELIVERY_ERROR_TOO_LARGE,
    DELIVERY_FAILED,
    DELIVERY_SENDING,
    DELIVERY_SENT,
    DELIVERY_STALE_AFTER_SECONDS,
    ECOPY_APPROVED,
    ECOPY_DECLINED,
    ECOPY_EVENT_APPROVED,
    ECOPY_EVENT_DECLINED,
    ECOPY_EVENT_DELIVERED,
    ECOPY_EVENT_DELIVERY_ATTEMPTED,
    ECOPY_EVENT_DELIVERY_FAILED,
    ECOPY_EVENT_REQUESTED,
    ECOPY_MAX_ATTACHMENT_BYTES,
    ECOPY_PENDING,
    ECOPY_SEND_TIMEOUT_SECONDS,
    LIFECYCLE_FINALIZED,
)
from ..models import Document, EcopyEvent, EcopyRequest
from ..repository import DocumentRepository
from ..schemas import (
    EcopyEventResponse,
    EcopyRequestListResponse,
    EcopyRequestResponse,
)


class DocumentEcopy:
    def __init__(self, session: AsyncSession, email: EmailProvider) -> None:
        self.session = session
        self.email = email
        self.repo = DocumentRepository(session)
        self.users = UserRepository(session)
        self.access = DocumentAccess(session)
        self.notifications = NotificationService(session)

    # === Client ===

    async def request(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> EcopyRequestResponse:
        """Ask the owner for the unwatermarked signed copy of a finalized document."""
        document = await self.access.authorize(user, document_id)
        if document.user_id == user.user_id:
            raise BadRequestError(
                "You own this document; its signed copy is already yours to download."
            )
        if document.lifecycle != LIFECYCLE_FINALIZED or document.signed_copy_id is None:
            raise ConflictError("Only a finalized document's signed copy can be requested.")
        if await self.repo.get_pending_ecopy(document_id, user.user_id):
            raise ConflictError("You already have a pending request for this document.")
        if document.document_hash and await self.repo.has_approved_ecopy(
            document_id, user.user_id, document.document_hash
        ):
            raise ConflictError("Your request for this copy was already approved.")

        request = EcopyRequest(document_id=document_id, client_id=user.user_id)
        try:
            # The pending-request index settles a race the check above lost.
            async with self.session.begin_nested():
                self.session.add(request)
                await self.session.flush()
        except IntegrityError:
            raise ConflictError("You already have a pending request for this document.")
        self._event(request, ECOPY_EVENT_REQUESTED, user.user_id)

        client = await self.users.get_by_id(user.user_id)
        await self.notifications.create(
            user_id=document.user_id,
            type=NotificationType.ECOPY_REQUESTED,
            title="E-copy requested",
            body=f"{_name(client)} asked for an e-copy of '{document.file_name}'.",
            event_metadata=_metadata(request),
        )
        await self.session.commit()
        log.info(
            "document.ecopy.requested",
            document_id=str(document_id),
            request_id=str(request.id),
            client_id=str(user.user_id),
        )
        return await self._response(request, document)

    async def list_for_document(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> EcopyRequestListResponse:
        """The owner sees every request on the document; a party sees their own."""
        document = await self.access.authorize(user, document_id)
        is_owner = document.user_id == user.user_id
        requests = await self.repo.list_ecopy_requests(
            document_id=document_id,
            client_id=None if is_owner else user.user_id,
        )
        return await self._list(requests)

    async def list_mine(self, user: AuthenticatedUser) -> EcopyRequestListResponse:
        """Every request this user made, on documents they can still read."""
        requests = await self.repo.list_ecopy_requests(client_id=user.user_id)
        readable = []
        for r in requests:
            if await self.repo.is_party(user.user_id, r.document_id):
                readable.append(r)
        return await self._list(readable)

    async def list_for_owner(
        self, user: AuthenticatedUser, status: str | None = None
    ) -> EcopyRequestListResponse:
        """Requests on every document this lawyer owns — the review queue."""
        requests = await self.repo.list_ecopy_requests(
            owner_id=user.user_id, status=status
        )
        return await self._list(requests)

    # === Owner ===

    async def approve(
        self, user: AuthenticatedUser, document_id: uuid.UUID, request_id: uuid.UUID
    ) -> EcopyRequestResponse:
        """Approve the current signed copy for this client, then email it."""
        document, request = await self._pending(user, document_id, request_id)
        if document.lifecycle != LIFECYCLE_FINALIZED or document.signed_copy_id is None:
            raise ConflictError("The document has no finalized signed copy to approve.")
        if not await self.repo.is_party(request.client_id, document_id):
            raise ConflictError("The client no longer has access to this document.")

        now = datetime.now(timezone.utc)
        request.status = ECOPY_APPROVED
        request.decided_at = now
        request.decided_by = user.user_id
        request.approved_copy_id = document.signed_copy_id
        request.approved_sha256 = document.document_hash
        request.delivery_status = DELIVERY_SENDING
        request.delivery_started_at = now
        request.delivery_attempts = 1
        self._event(request, ECOPY_EVENT_APPROVED, user.user_id)
        self._event(request, ECOPY_EVENT_DELIVERY_ATTEMPTED, user.user_id)
        await self.notifications.create(
            user_id=request.client_id,
            type=NotificationType.ECOPY_APPROVED,
            title="E-copy approved",
            # Says nothing about the email: whether it went out is the
            # request's delivery_status, not a promise made before sending.
            body=(
                f"Your request for an e-copy of '{document.file_name}' was approved. "
                "You can download it in LexChain."
            ),
            event_metadata=_metadata(request),
        )
        # The decision stands on its own: it is committed before the email is
        # tried, so a failed send leaves an approval that is owed a delivery.
        await self.session.commit()
        log.info(
            "document.ecopy.approved",
            document_id=str(document_id),
            request_id=str(request.id),
            owner_id=str(user.user_id),
        )
        await self._deliver(request, document, user.user_id)
        return await self._response(request, document)

    async def decline(
        self,
        user: AuthenticatedUser,
        document_id: uuid.UUID,
        request_id: uuid.UUID,
        reason: str | None = None,
    ) -> EcopyRequestResponse:
        document, request = await self._pending(user, document_id, request_id)
        reason = (reason or "").strip() or None
        request.status = ECOPY_DECLINED
        request.decided_at = datetime.now(timezone.utc)
        request.decided_by = user.user_id
        request.decline_reason = reason
        self._event(request, ECOPY_EVENT_DECLINED, user.user_id)
        await self.notifications.create(
            user_id=request.client_id,
            type=NotificationType.ECOPY_DECLINED,
            title="E-copy declined",
            body=f"Your request for an e-copy of '{document.file_name}' was declined."
            + (f" Reason: {reason}" if reason else ""),
            event_metadata=_metadata(request),
        )
        await self.session.commit()
        log.info(
            "document.ecopy.declined",
            document_id=str(document_id),
            request_id=str(request.id),
            owner_id=str(user.user_id),
        )
        return await self._response(request, document)

    async def retry_delivery(
        self, user: AuthenticatedUser, document_id: uuid.UUID, request_id: uuid.UUID
    ) -> EcopyRequestResponse:
        """Send the approved copy again after a failed (or abandoned) attempt."""
        document = await self.access.authorize(user, document_id, ACTION_REVIEW_ECOPY)
        request = await self.repo.get_ecopy_request(document_id, request_id)
        if request is None:
            raise NotFoundError("E-copy request not found")
        if not await self.repo.claim_ecopy_delivery(request_id):
            await self.session.refresh(request)
            raise ConflictError(_why_not_retryable(request))
        await self.session.refresh(request)
        self._event(request, ECOPY_EVENT_DELIVERY_ATTEMPTED, user.user_id)
        await self.session.commit()
        await self._deliver(request, document, user.user_id)
        return await self._response(request, document)

    # ========== helpers =============

    async def _pending(
        self, user: AuthenticatedUser, document_id: uuid.UUID, request_id: uuid.UUID
    ) -> tuple[Document, EcopyRequest]:
        """The owner's document and the still-PENDING request, locked for the decision."""
        document = await self.access.authorize(user, document_id, ACTION_REVIEW_ECOPY)
        request = await self.repo.get_ecopy_request(
            document_id, request_id, for_update=True
        )
        if request is None:
            raise NotFoundError("E-copy request not found")
        if request.status != ECOPY_PENDING:
            raise ConflictError(f"This request was already {request.status.lower()}.")
        return document, request

    async def _deliver(
        self, request: EcopyRequest, document: Document, actor_id: uuid.UUID
    ) -> None:
        """Email the approved copy and record how it went. Never raises for a
        failed send: the outcome is the request's delivery state."""
        error, client = await self._send(request, document)
        if error is None and client is not None:
            request.delivery_status = DELIVERY_SENT
            request.delivered_at = datetime.now(timezone.utc)
            request.delivered_to = client.email
            request.delivery_error = None
            self._event(request, ECOPY_EVENT_DELIVERED, actor_id, client.email)
        else:
            request.delivery_status = DELIVERY_FAILED
            request.delivery_error = error
            self._event(request, ECOPY_EVENT_DELIVERY_FAILED, actor_id, error)
            await self.notifications.create(
                user_id=document.user_id,
                type=NotificationType.ECOPY_DELIVERY_FAILED,
                title="E-copy email not sent",
                body=(
                    f"The approved e-copy of '{document.file_name}' could not be "
                    "emailed. Open the request to see why and retry."
                ),
                event_metadata=_metadata(request),
            )
        await self.session.commit()
        log.info(
            "document.ecopy.delivery",
            document_id=str(document.id),
            request_id=str(request.id),
            attempt=request.delivery_attempts,
            status=request.delivery_status,
            error=error,
        )

    async def _send(
        self, request: EcopyRequest, document: Document
    ) -> tuple[str | None, User | None]:
        """Try one send. Returns (error code or None, the client)."""
        client = await self.users.get_by_id(request.client_id)
        if (
            client is None
            or not client.is_active
            or not await self.repo.is_party(request.client_id, document.id)
        ):
            return DELIVERY_ERROR_ACCESS, client
        if not client.is_email_verified:
            return DELIVERY_ERROR_EMAIL_UNVERIFIED, client

        await self.session.refresh(document, ["signed_copy"])
        copy = document.signed_copy
        # The approval covers the bytes it was given for. A document whose
        # file has changed since must be approved again, not sent.
        if copy is None or document.document_hash != request.approved_sha256:
            return DELIVERY_ERROR_INTEGRITY, client
        if copy.size_bytes > ECOPY_MAX_ATTACHMENT_BYTES:
            return DELIVERY_ERROR_TOO_LARGE, client
        try:
            content = await get_storage_client().download(copy.storage_url)
        except Exception as e:
            log.error(
                "document.ecopy.download_failed",
                request_id=str(request.id),
                error=type(e).__name__,
            )
            return DELIVERY_ERROR_STORAGE, client
        if hashlib.sha256(content).hexdigest() != request.approved_sha256:
            return DELIVERY_ERROR_INTEGRITY, client

        owner = await self.users.get_by_id(document.user_id)
        html, text = build_ecopy_email(document.file_name, _name(owner))
        message = EmailMessage(
            to=client.email,
            subject=f"Your e-copy of {document.file_name}",
            html=html,
            text=text,
            attachments=[
                EmailAttachment(filename=_filename(document.file_name), content=content)
            ],
        )
        try:
            sent = await asyncio.wait_for(
                self.email.send(message), ECOPY_SEND_TIMEOUT_SECONDS
            )
        except Exception as e:
            log.error(
                "document.ecopy.send_failed",
                request_id=str(request.id),
                error=type(e).__name__,
            )
            sent = False
        return (None if sent else DELIVERY_ERROR_PROVIDER), client

    def _event(
        self,
        request: EcopyRequest,
        action: str,
        actor_id: uuid.UUID | None,
        detail: str | None = None,
    ) -> None:
        self.session.add(
            EcopyEvent(
                request_id=request.id, action=action, actor_id=actor_id, detail=detail
            )
        )

    async def _list(self, requests: list[EcopyRequest]) -> EcopyRequestListResponse:
        responses = []
        for r in requests:
            document = await self.repo.get_by_id(r.document_id)
            if document is not None:  # Cascades with the request; never missing.
                responses.append(await self._response(r, document))
        return EcopyRequestListResponse(requests=responses, total=len(responses))

    async def _response(
        self, request: EcopyRequest, document: Document
    ) -> EcopyRequestResponse:
        await self.session.flush()
        await self.session.refresh(request, ["events"])
        client = await self.users.get_by_id(request.client_id)
        approved_and_current = (
            request.status == ECOPY_APPROVED
            and document.document_hash is not None
            and document.document_hash == request.approved_sha256
        )
        return EcopyRequestResponse(
            id=request.id,
            document_id=request.document_id,
            file_name=document.file_name,
            client_id=request.client_id,
            client_name=_name(client),
            client_email=client.email if client else "",
            status=request.status,
            requested_at=request.created_at,
            decided_at=request.decided_at,
            decided_by=request.decided_by,
            decline_reason=request.decline_reason,
            approved_copy_id=request.approved_copy_id,
            delivery_status=request.delivery_status,
            delivery_attempts=request.delivery_attempts,
            delivered_at=request.delivered_at,
            delivered_to=request.delivered_to,
            delivery_error=request.delivery_error,
            can_download=approved_and_current
            and await self.repo.is_party(request.client_id, request.document_id),
            can_retry_delivery=_retryable(request),
            events=[EcopyEventResponse.model_validate(e) for e in request.events],
        )


def _retryable(request: EcopyRequest) -> bool:
    """Mirrors `DocumentRepository.claim_ecopy_delivery`."""
    if request.status != ECOPY_APPROVED:
        return False
    if request.delivery_status == DELIVERY_FAILED:
        return True
    if request.delivery_status == DELIVERY_SENDING and request.delivery_started_at:
        age = datetime.now(timezone.utc) - request.delivery_started_at
        return age.total_seconds() > DELIVERY_STALE_AFTER_SECONDS
    return False


def _why_not_retryable(request: EcopyRequest) -> str:
    if request.status != ECOPY_APPROVED:
        return "Only an approved request can be sent."
    if request.delivery_status == DELIVERY_SENT:
        return "This e-copy was already delivered."
    return "A send for this request is already in progress."


def _name(user: User | None) -> str:
    return f"{user.f_name} {user.l_name}".strip() if user else ""


def _metadata(request: EcopyRequest) -> dict:
    return {"document_id": str(request.document_id), "request_id": str(request.id)}


def _filename(display_name: str) -> str:
    """A safe attachment name from the document's display name."""
    stem = re.sub(r"[^\w\-. ]+", "", display_name).strip()[:100] or "signed-copy"
    return stem if stem.lower().endswith(".pdf") else f"{stem}.pdf"
