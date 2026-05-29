from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.infrastructure.database import get_db
from .service import UserService

def get_user_service(
    db: AsyncSession = Depends(get_db),
) -> UserService:
    return UserService(db)
