from app.infrastructure.database import get_db
from app.infrastructure.google import build_google_oauth
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from .service import GoogleConnectionService


def get_google_service(db: AsyncSession = Depends(get_db)) -> GoogleConnectionService:
    return GoogleConnectionService(db, build_google_oauth())
