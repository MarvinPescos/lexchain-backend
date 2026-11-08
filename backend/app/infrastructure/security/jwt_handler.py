from typing import Optional, TypedDict, cast

import jwt
from app.core import settings
from app.infrastructure.observability.logging_setup import log
from app.shared.errors.exceptions import ServiceUnavailableError, UnauthorizedError
from jwt import PyJWKClient
from jwt.exceptions import (
    ExpiredSignatureError,
    InvalidTokenError,
    PyJWKClientConnectionError,
    PyJWKClientError,
)


# This is what the payloads contains
class JWTPayload(TypedDict):
    sub: str  # Supabase user id
    aud: str  # "authenticated"
    exp: int  # Expiration timestamp
    iat: int  # Issued at timestamp
    email: Optional[str]  # User email
    # Postgres role, not the app role. Supabase sets it to "authenticated" at
    # sign-up and nothing here ever changes it, so it reads the same for a
    # lawyer and a client. Authorization comes from users.role, read from the
    # database in dependencies.get_current_user — never from this claim.
    # Listed because the token carries it, not because anything uses it.
    role: Optional[str]
    # Authenticator assurance level: "aal1" after a password alone, "aal2"
    # once a TOTP code has been verified on this session. get_current_user
    # refuses aal1 everywhere except the MFA endpoints.
    aal: Optional[str]
    # How the session was established, e.g. [{"method": "password", ...},
    # {"method": "totp", ...}]. Carried for logging; aal is what is enforced.
    amr: Optional[list[dict]]


# Cache the JWKS client to avoid fetching keys on every request
_jwks_client: Optional[PyJWKClient] = None


def _get_jwks_client() -> PyJWKClient:
    """Get or create cached JWKS client"""
    global _jwks_client
    if _jwks_client is None:
        _jwks_url = f"{settings.SUPABASE_URL}/auth/v1/.well-known/jwks.json"
        # Added 2026-09-12 16:53 PST — bound the blocking fetch and cache keys.
        # get_signing_key_from_jwt() calls urllib.request.urlopen (sync), and
        # PyJWT defaults to timeout=30, so a hung Supabase stalls the caller for
        # 30s. cache_keys defaults to False, which re-parses the JWK set into
        # key objects on every request; the JWK-set cache above it only avoids
        # the network hop (lifespan=300).
        _jwks_client = PyJWKClient(_jwks_url, cache_keys=True, timeout=5)
    return _jwks_client


def verify_jwt_token(token: str) -> JWTPayload:
    """
    Verify Supabase JWT token using ES256 (asymmetric).
    Fetches public keys from Supabase JWKS endpoint.

    Args:
        token: JWT token string
    Returns:
        Dict containing user information from token
    Raises:
        UnauthorizedError: If the token is invalid, expired, or malformed.
        ServiceUnavailableError: If the JWKS endpoint cannot be reached.
        Anything else: deliberately not caught. A failure that is neither of
            the above is a bug here, not an authentication problem, and
            main.unhandled_exception_handler answers it as a 500 with the
            request path logged — where a blind `except Exception` used to
            report it as 401 "Authentication failed" and hide it.
    """
    try:
        # Get the siging key from JWKS
        jwks_client = _get_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token)

        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256"],
            audience="authenticated",
            options={"verify_signature": True, "verify_exp": True, "verify_aud": True},
        )
        # Added 2026-09-12 16:53 PST — JWTPayload validates nothing.
        # It is a TypedDict, i.e. a plain dict at runtime, so a token with no
        # "sub" passed straight through and surfaced downstream as
        # get_by_supabase_id(None) -> "Account is not provisioned", telling the
        # user to sign up when the real problem is a malformed token.
        # InvalidTokenError is caught below and reported as one.
        if not payload.get("sub"):
            raise InvalidTokenError("Token is missing the 'sub' claim")

        log.debug("jwt.verified.token", user=payload.get("sub"))
        # cast, not JWTPayload(**payload): a TypedDict cannot be built by **
        # expansion from dict[str, Any], and the call was copying the dict to
        # produce something already structurally identical.
        return cast(JWTPayload, payload)

    except ExpiredSignatureError:
        log.warning("jwt.expired.token")
        raise UnauthorizedError("Token has expired")
    # Added 2026-09-12 16:53 PST — an unreachable JWKS endpoint is not a bad
    # token. PyJWKClientConnectionError subclasses PyJWKClientError, so the
    # handler below used to swallow it and answer 401 "Invalid authentication
    # token" whenever Supabase was unreachable or DNS blipped — which tells the
    # frontend the session is dead and logs every user out at once, for a
    # transient network fault. Must stay ABOVE the PyJWKClientError catch:
    # being a subclass, it never runs if it comes after.
    except PyJWKClientConnectionError as e:
        log.error("jwt.jwks.unreachable", error=str(e))
        raise ServiceUnavailableError(
            "Authentication service is temporarily unavailable. Please try again.",
            retry_after=30,
        )
    except (InvalidTokenError, PyJWKClientError) as e:
        log.warning("jwt.invalid.token", error=str(e))
        raise UnauthorizedError("Invalid authentication token")
