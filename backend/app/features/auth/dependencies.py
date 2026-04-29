from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.infrastructure.database import get_db
from .service import AuthService

def get_auth_service(db: AsyncSession = Depends(get_db)) -> AuthService:
    """Dependency injection (provides AuthService with database session)"""
    return AuthService(db)