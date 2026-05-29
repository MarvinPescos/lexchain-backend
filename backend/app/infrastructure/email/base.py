from typing import Protocol
from pydantic import BaseModel

class EmailMessage(BaseModel):
    to: str
    subject: str
    html: str
    text: str | None = None


class EmailProvider(Protocol):
    async def send(self, message: EmailMessage) -> bool:
        ...