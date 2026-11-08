import asyncio
import uuid

from app.infrastructure.database import get_db
from app.infrastructure.observability.logging_setup import log
from app.shared.constants import ROLE_LAWYER
from app.shared.errors.exceptions import ForbiddenError, UnauthorizedError
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .jwt_handler import verify_jwt_token

# Security scheme for swagger
security_scheme = HTTPBearer(
    scheme_name="Bearer Token", description="Enter your Supabase JWT token"
)


AAL_MFA = "aal2"

_MFA_REQUIRED = (
    "Multi-factor authentication required. "
    "Enter the code from your authenticator app."
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
    # Added 2026-09-12 17:05 PST — dropped `raw_payload`.
    # It carried the whole decoded token onto every AuthenticatedUser and no
    # caller ever read it. Its one live consequence was exposing the token's
    # `role` claim (the Postgres role — "authenticated" for everyone; see
    # JWTPayload in jwt_handler.py) one attribute away from the guards, where
    # it could be mistaken for the field below. `role` here is the app role,
    # read from users.role.
    role: str = Field(..., description="User role (from users.role)")
    # Read from the token's `aal` claim. get_current_user only ever hands out
    # aal2; get_first_factor_user (the MFA endpoints) may hand out aal1.
    aal: str | None = Field(default=None, description="Authenticator assurance level")

    def is_lawyer(self) -> bool:
        """Check if user holds lawyer authority.

        There is no separate admin tier: the lawyer is the primary authority
        and also runs the admin panel.
        """
        return self.role == ROLE_LAWYER

    # Added 2026-09-12 17:20 PST — ConfigDict, not `class Config`.
    # Pydantic deprecated class-based config in 2.0 and drops it in 3.0; it
    # raises PydanticDeprecatedSince20 on import today. json_schema_extra is
    # also the v2 spelling of schema_extra — under the old name the example was
    # silently ignored and never reached the OpenAPI schema.
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "user_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "email": "user@example.com",
                "is_active": True,
                "role": "user",
            }
        },
    )


async def get_first_factor_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    session: AsyncSession = Depends(get_db),
) -> AuthenticatedUser:
    """
    FastApi dependecy: get user from token and ensure they exist in db.

    Accepts a password-only (aal1) session. Only the MFA endpoints and sign-out
    use this directly; everything else goes through get_current_user.
    """
    # Added 2026-09-07 13:46 PST — raise the app's own errors, not HTTPException.
    # These four guards were the only auth code answering in FastAPI's
    # {"detail": ...} shape while the rest of the API answered in the
    # error_type/message shape, so the frontend had to read the message from a
    # different key depending on which error it hit. UnauthorizedError now
    # carries WWW-Authenticate itself, which is the only reason HTTPException
    # was needed here. The try/except that wrapped this block went with it: it
    # existed to convert UnauthorizedError from verify_jwt_token into an
    # HTTPException, and would otherwise swallow the ones raised below.
    token = credentials.credentials

    # Added 2026-09-12 16:53 PST — keep the key fetch off the event loop.
    # verify_jwt_token reaches urllib.request.urlopen inside PyJWT, which is
    # synchronous: whenever the JWK-set cache expires (every 300s) the next
    # request made that call inline and froze *every* in-flight request for its
    # duration, not just its own.
    payload = await asyncio.to_thread(verify_jwt_token, token)

    from app.features.users.repository import UserRepository

    user_repo = UserRepository(session)
    # payload is a JWTPayload and jwt_handler guarantees "sub" is present,
    # so this is a str rather than Any | None.
    supabase_user_id = payload["sub"]

    # Added 2026-09-07 00:01 PST — authentication is a read, not a write.
    # This called sync_from_supabase(), which runs an UPDATE on the users
    # row and flushes; get_db commits unconditionally, so *every*
    # authenticated request — including plain GETs — opened a write
    # transaction and committed. That meant row-level lock contention
    # proportional to a user's request rate, updated_at churn that made the
    # column meaningless, and a cheap amplifier where reads cost writes.
    # It also wrote `payload.get("email") or ""` back to a UNIQUE NOT NULL
    # column, so a token with no email claim blanked the row and the next
    # one collided during an auth check.
    # Provisioning belongs on sign-up/sign-in, which already call
    # sync_from_supabase with the columns this path could not supply —
    # f_name/l_name are NOT NULL, so the create branch here raised a 500 for
    # any genuinely new user anyway. That case is now a clean 401.
    user_db = await user_repo.get_by_supabase_id(supabase_user_id)
    if not user_db:
        log.warning(
            "auth.rejected.unprovisioned_user",
            supabase_user_id=supabase_user_id,
        )
        raise UnauthorizedError("Account is not provisioned. Please sign in again.")

    role = user_db.role

    # Added 2026-09-06 14:42 PST — enforce the account off-switch.
    # `is_active` was read into AuthenticatedUser but never checked by any
    # guard, so a deactivated account kept full access: require_auth returns
    # unconditionally and require_lawyer only looks at `role`. Deactivating a
    # user changed nothing but the boolean shown in the admin user list.
    # Checked here so it covers every authenticated route at once, and takes
    # effect on the next request (the user row is re-read per request).
    # 403, not 401: the token is valid and the identity is established —
    # re-authenticating will not help, sign_in refuses these accounts too.
    if not user_db.is_active:
        log.warning(
            "auth.rejected.inactive_account",
            user_id=user_db.id,
            supabase_user_id=supabase_user_id,
        )
        raise ForbiddenError("Account is deactivated. Contact your administrator.")

    log.debug(f"Authenticated user: {user_db.id}")

    return AuthenticatedUser(
        user_id=user_db.id,
        # The stored email is now the source of truth — nothing on this path
        # writes the JWT claim back, so reading it here would let the two
        # disagree silently.
        email=user_db.email,
        is_active=user_db.is_active,
        role=role,
        aal=payload.get("aal"),
    )


# Added 2026-11-08 19:30 PST — a password alone no longer opens a session.
# Sign-in returns an aal1 token; only verifying a TOTP code upgrades it to
# aal2. Checked here so every route behind require_auth / require_lawyer is
# covered at once, the same way the is_active guard is. A token without the
# claim is treated as aal1. 403, not 401: the token is good and the frontend
# must not clear it — it is exactly what POST /auth/mfa/verify needs. The
# details tell the client this is the step-up case, not a permissions one.
async def get_current_user(
    user: AuthenticatedUser = Depends(get_first_factor_user),
) -> AuthenticatedUser:
    """FastApi dependecy: the caller, signed in with password *and* TOTP."""
    if user.aal != AAL_MFA:
        log.info("auth.rejected.mfa_required", user_id=user.user_id, aal=user.aal)
        raise ForbiddenError(_MFA_REQUIRED, details={"mfa_required": True})
    return user


def require_auth(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Simplified Dependency alias for requiring authentication"""
    return user


def require_lawyer(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Require lawyer authority — the only privileged role"""
    if not user.is_lawyer():
        raise ForbiddenError("Only lawyer can perform this action")
    return user
