from time import get_clock_info
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic_core.core_schema import date_schema
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.features.users.repository import UserRepository
from .jwt_handler import verify_jwt_token
from app.infrastructure.database import get_db
from app.shared.errors.exceptions import UnauthorizedError
from app.infrastructure.observability.logging_setup import log

# Security scheme for swagger
security_scheme = HTTPBearer(
    scheme_name="Bearer Token",
    description="Enter your Supabase JWT token"
)

# Schema for authenticated user!
class AuthenticatedUser(BaseModel):
    """
        Represents an authenticated user in the system
        Domain model (not an API scheme)
    """     

    user_id: str = Field(..., description="Local database user ID")
    email: str | None = Field(..., description="User email")
    is_active: bool = Field(..., description="Status of account")
    role: str = Field(..., description="User role")
    raw_payload: dict = Field(..., description=f"Raw JWT Payload")

    def is_admin(self) -> bool:
        """Check if user is admin"""
        return self.role == "admin"

    class Config:
        extra="forbid"
        schema_extra = {
            "example": {
                "user_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "email": "user@example.com",
                "role": "user",
                "raw_payload": {}
            }
        }

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    session: AsyncSession = Depends(get_db)
) -> AuthenticatedUser:
    """
        FastApi dependecy: get user from token and ensure they exist in db
    """
    try:

        token = credentials.credentials

        payload = verify_jwt_token(token)

        user_repo = UserRepository(session)
        supabase_user_id = payload.get("sub")
        email = payload.get("email")
        user_db = await user_repo.sync_from_supabase(supabase_user_id, email)
        role = payload.get("app_metadata", {}).get("role", "user")

        log.debug(f"Authenticated user: {user_db.id}")

        return AuthenticatedUser(
            user_id=str(user_db.id),
            email=email,
            is_active=user_db.is_active,
            role=role,
            raw_payload=payload
        )
    except UnauthorizedError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"}
        )

def require_auth(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    """ Simplified Dependency alias for requiring authentication"""
    return user

def require_admin(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    """Require admin role"""

    if not user.is_admin():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin access required"
        )
    return user