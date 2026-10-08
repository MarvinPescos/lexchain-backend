import asyncio

import resend
from app.core import settings
from app.infrastructure.observability.logging_setup import log

from .base import EmailMessage


class ResendEmailProvider:
    def __init__(self) -> None:
        resend.api_key = settings.RESEND_API_KEY

    async def send(self, message: EmailMessage) -> bool:
        try:
            payload = {
                "from": f"LexChain <{settings.RESEND_FROM_EMAIL}>",
                "to": [message.to],
                "subject": message.subject,
                "html": message.html,
            }
            if message.text:
                payload["text"] = message.text

            # The SDK takes the params as one dict, not keyword arguments.
            await asyncio.to_thread(resend.Emails.send, payload)

            log.info("email.sent", to=message.to, subject=message.subject)
            return True

        except Exception as e:
            log.error("email.send.failed", to=message.to, error=str(e))
            return False
