from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.infrastructure.database import get_db
from .service import AdminService
from app.infrastructure.email import build_email_provider, EmailProvider


def get_email_provider() -> EmailProvider:
    return build_email_provider()


def get_admin_service(
    db: AsyncSession = Depends(get_db),
    email: EmailProvider = Depends(get_email_provider),
) -> AdminService:
    return AdminService(db, email)