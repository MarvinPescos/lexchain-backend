"""Email for backup problems. Logs alone mean a stopped backup goes unnoticed."""

from app.core import settings
from app.infrastructure.email import EmailMessage, build_email_provider


async def send_backup_alert(subject: str, body: str) -> None:
    if not settings.BACKUP_ALERT_EMAIL:
        return
    await build_email_provider().send(
        EmailMessage(
            to=settings.BACKUP_ALERT_EMAIL,
            subject=f"LexChain: {subject}",
            html=f"<p>{body}</p>",
            text=body,
        )
    )
