from typing import TypedDict, Optional
from httpx import StreamError, stream
from jwt import PyJWKClient, jwks_client
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError, PyJWKClientError
import jwt 

from app.core import settings 
from app.shared.errors.exceptions import UnauthorizedError
from app.infrastructure.observability.logging_setup import log

# This is what the payloads contains
class JWTPayload(TypedDict):
    sub: str                # Supabase user id
    aud: str                # "authenticated"
    exp: int                # Expiration timestamp
    iat: int                # Issued at timestamp
    email: Optional[str]    # User email
    role: Optional[str]     # User role

# Cache the JWKS client to avoid fetching keys on every request
_jwks_client: Optional[PyJWKClient] = None

def _get_jwks_client() -> PyJWKClient:
    """ Get or create cached JWKS client"""
    global _jwks_client
    if _jwks_client is None:
        _jwks_url = f"{settings.SUPABASE_URL}/auth/v1/.well-known/jwks.json"
        _jwks_client = PyJWKClient(_jwks_url)
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
            UnauthorizedError: If token is invalid or expired
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
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": True
            }
        )
        log.debug("jwt.verified.token", user=payload.get('sub'))
        return payload
    
    except ExpiredSignatureError:
        log.warning("jwt.expired.token")
        raise UnauthorizedError("Token has expired")
    except (InvalidTokenError, PyJWKClientError) as e:
        log.warning("jwt.invalid.token", error=str(e))
        raise UnauthorizedError("Invalid authentication token")
    except UnauthorizedError:
        raise
    except Exception as e:
        log.error("jwt.verification.error", error=str(e), exc_info=True)
        raise UnauthorizedError("Authentication failed")

