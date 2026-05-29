from .base import EmailProvider, EmailMessage
from .mock_provider import MockEmailProvider
from .resend_provider import ResendEmailProvider
from .templates import build_invitation_email, build_upgrade_email
from app.core import settings

def build_email_provider() -> EmailProvider:
    if settings.ENVIRONMENT == "production":
        return MockEmailProvider()
    return ResendEmailProvider()


__all__ = ["EmailProvider", "EmailMessage", "build_email_provider", "build_invitation_email", "build_upgrade_email"]