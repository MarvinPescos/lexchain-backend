import uuid
from typing import Any

from app.infrastructure.database import get_db
from app.infrastructure.observability.logging_setup import log
from app.shared.errors.exceptions import UnauthorizedError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .jwt_handler import verify_jwt_token

# Security scheme for swagger
security_scheme = HTTPBearer(
    scheme_name="Bearer Token", description="Enter your Supabase JWT token"
)


# Schema for authenticated user!
class AuthenticatedUser(BaseModel):
    """
    Represents an authenticated user in the system
    Domain model (not an API scheme)
    """

    user_id: uuid.UUID = Field(..., description="Local database user ID")
    email: str | None = Field(..., description="User email")
    is_active: bool = Field(..., description="Status of account")
    role: str = Field(..., description="User role")
    raw_payload: dict = Field(..., description="Raw JWT Payload")

    def is_admin(self) -> bool:
        """Check if user is admin"""
        return self.role == "admin"

    def is_lawyer(self) -> bool:
        """Check if user is a lawyer"""
        return self.role == "lawyer"

    class Config:
        extra = "forbid"
        schema_extra = {
            "example": {
                "user_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "email": "user@example.com",
                "role": "user",
                "raw_payload": {},
            }
        }


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    session: AsyncSession = Depends(get_db),
) -> AuthenticatedUser:
    """
    FastApi dependecy: get user from token and ensure they exist in db
    """
    try:
        token = credentials.credentials

        payload: dict | Any = verify_jwt_token(token)

        from app.features.users.repository import UserRepository
        user_repo = UserRepository(session)
        supabase_user_id = payload.get("sub")
        email = payload.get("email") or ""
        user_db = await user_repo.sync_from_supabase(supabase_user_id, email)
        role = user_db.role

        log.debug(f"Authenticated user: {user_db.id}")

        return AuthenticatedUser(
            user_id=str(user_db.id),
            email=email,
            is_active=user_db.is_active,
            role=role,
            raw_payload=payload,
        )
    except UnauthorizedError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_auth(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Simplified Dependency alias for requiring authentication"""
    return user


# TODO: search if admin does have the rights to have lawyer level of power
def require_admin(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Require admin role"""

    if not user.is_admin():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin access required"
        )
    return user


def require_lawyer(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Require lawyer role"""
    if not user.is_lawyer() and not user.is_admin():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only lawyer can perform this action",
        )
    return user
