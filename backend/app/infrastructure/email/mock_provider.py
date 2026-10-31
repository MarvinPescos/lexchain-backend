from app.infrastructure.observability.logging_setup import log
from .base import EmailMessage

class MockEmailProvider:
    async def send(self, message: EmailMessage) -> bool:
        log.info(
            "email.sent.mock",
            to=message.to,
            subject=message.subject,
            attachments=[a.filename for a in message.attachments],
        )
        return True
