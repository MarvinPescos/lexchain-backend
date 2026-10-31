from typing import Protocol
from pydantic import BaseModel


class EmailAttachment(BaseModel):
    filename: str
    content: bytes
    content_type: str = "application/pdf"


class EmailMessage(BaseModel):
    to: str
    subject: str
    html: str
    text: str | None = None
    attachments: list[EmailAttachment] = []


class EmailProvider(Protocol):
    async def send(self, message: EmailMessage) -> bool:
        ...
