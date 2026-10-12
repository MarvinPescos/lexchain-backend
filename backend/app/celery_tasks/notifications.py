import uuid

from app.celery_app import celery_app
from app.celery_tasks.runner import run_async
from app.features.notifications.models import NotificationType
from app.features.notifications.repository import NotificationRepository
from app.infrastructure.database import AsyncSessionLocal


async def _create_notification(user_id, type, title, body, metadata):
    import app.all_models  # noqa: F401
    async with AsyncSessionLocal() as session:
        repo = NotificationRepository(session)
        await repo.create(
            user_id=uuid.UUID(user_id),
            type=type,
            title=title,
            body=body,
            event_metadata=metadata,
        )
        await session.commit()


@celery_app.task(name="notifications.party_added")
def handle_party_added(
    party_user_id: uuid.UUID, owner_name: str, document_title: str, document_id: str
):
    run_async(
        _create_notification(
            user_id=party_user_id,
            type=NotificationType.PARTY_ADDED,
            title="You were added to a document.",
            body=f"{owner_name} added you to '{document_title}'",
            metadata={"document_id": document_id},
        )
    )


@celery_app.task(name="notifications.document_recorded")
def handle_document_recorded(
    user_id: uuid.UUID,
    document_title: str,
    document_id: str,
    tx_hash: str,
):
    run_async(
        _create_notification(
            user_id=user_id,
            type=NotificationType.DOCUMENT_RECORDED,
            title="Document recorded on-chain",
            body=f"'{document_title}' has been anchored to the blockchain.",
            metadata={"document_id": document_id, "tx_hash": tx_hash},
        )
    )


@celery_app.task(name="notifications.document_processed")
def handle_document_processed(
    user_id: uuid.UUID, document_title: str, document_id: str, status: str
):
    # Three outcomes now: OCR finished and needs the issuer, enrichment finished,
    # or something failed. Anything not explicitly a success must not be reported
    # as a failure, or the issuer is told a document broke when it is simply
    # waiting on them.
    if status == "awaiting_review":
        notification_type = NotificationType.DOCUMENT_PROCESSED
        title = "Document ready for review"
        body = f"'{document_title}' has been extracted and needs your review."
    elif status == "completed":
        notification_type = NotificationType.DOCUMENT_PROCESSED
        title = "Document processed"
        body = f"'{document_title}' is ready."
    else:
        notification_type = NotificationType.DOCUMENT_FAILED
        title = "Document processing failed"
        body = f"Processing failed for '{document_title}'."

    run_async(
        _create_notification(
            user_id=user_id,
            type=notification_type,
            title=title,
            body=body,
            metadata={"document_id": document_id, "status": status},
        )
    )


async def _send_draft_activity_email(user_id, subject, body, document_name, draft_url):
    import app.all_models  # noqa: F401
    from app.features.users.repository import UserRepository
    from app.infrastructure.email import (
        EmailMessage,
        build_draft_activity_email,
        build_email_provider,
    )
    from app.infrastructure.observability.logging_setup import log

    async with AsyncSessionLocal() as session:
        user = await UserRepository(session).get_by_id(uuid.UUID(user_id))
    if user is None or not user.is_active:
        return

    html, text = build_draft_activity_email(subject, body, document_name, draft_url)
    sent = await build_email_provider().send(
        EmailMessage(to=user.email, subject=subject, html=html, text=text)
    )
    if not sent:
        log.error("email.draft_activity.failed", user_id=user_id, subject=subject)


@celery_app.task(name="notifications.draft_activity_email")
def send_draft_activity_email(
    user_id: str, subject: str, body: str, document_name: str, draft_url: str | None
):
    """Email one recipient about a comment, reply or resolve on a draft."""
    run_async(
        _send_draft_activity_email(user_id, subject, body, document_name, draft_url)
    )
