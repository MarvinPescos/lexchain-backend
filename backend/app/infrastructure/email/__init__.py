from .base import EmailAttachment, EmailProvider, EmailMessage
from .mock_provider import MockEmailProvider
from .resend_provider import ResendEmailProvider
from .templates import (
    build_document_invite_email,
    build_draft_activity_email,
    build_ecopy_email,
    build_invitation_email,
    build_upgrade_email,
)
from app.core import settings

def build_email_provider() -> EmailProvider:
    # Was inverted (production got the mock, so production never sent mail).
    if not settings.USE_REAL_EMAIL:
        return MockEmailProvider()
    return ResendEmailProvider()


__all__ = [
    "EmailProvider",
    "EmailMessage",
    "EmailAttachment",
    "build_email_provider",
    "build_document_invite_email",
    "build_draft_activity_email",
    "build_ecopy_email",
    "build_invitation_email",
    "build_upgrade_email",
]