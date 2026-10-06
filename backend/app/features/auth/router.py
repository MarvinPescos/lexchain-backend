from app.infrastructure.security.dependencies import (
    AuthenticatedUser,
    require_auth,
    security_scheme,
)
from fastapi import APIRouter, Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials

# from app.infrastructure.cache import limiter, RateLimits
from .dependencies import get_auth_service
from .schemas import (
    MessageResponse,
    ResendVerificationRequest,
    SignInRequest,
    SignInResponse,
    SignUpRequest,
    SignUpResponse,
)
from .service import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post(
    "/signup",
    response_model=SignUpResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
    description="""
    Create a new user account with email, password, and name.

    - **Email Verification**: If enabled in Supabase, user must verify email before signing in.
    - **Name**: First and last name, 1-50 characters, letters/spaces/hyphens/apostrophes.
    - **Password**: Must be at least 8 characters with uppercase, lowercase, and digit.
    - **Rate Limit**: 5 requests per minute.
    """,
    responses={
        201: {"description": "User created successfully"},
        400: {"description": "Invalid input or user already exists"},
        429: {
            "description": "Too Many Requests - Rate limit exceeded",
            "content": {
                "application/json": {"example": {"error": "Rate limit exceeded"}}
            },
        },
        502: {"description": "Failed to connect to Supabase"},
    },
)
# @limiter.limit(RateLimits.AUTH_STRICT)
async def sign_up(
    request: Request,
    data: SignUpRequest,
    service: AuthService = Depends(get_auth_service),
) -> SignUpResponse:
    """
    Register a new user with Supabase and sync to local database.

    Args:
        request: The fastAPI request object.
        data: Payload containing user registration details.
        service: The authentication service.

    Returns:
        The created user's identity and confirmation status.

    Raises:
        BadRequestError: If the user already exists or sign-up fails.
        BadGatewayError: If Supabase is unreachable.
    """
    return await service.sign_up(data)


@router.post(
    "/signin",
    response_model=SignInResponse,
    status_code=status.HTTP_200_OK,
    summary="Sign in a user",
    description="""
    Authenticate a user with email and password.

    - **Returns**: Access token, refresh token, and token expiration time.
    - **Email Verification**: User must have verified their email to sign in.
    - **Rate Limit**: 5 requests per minute.
    """,
    responses={
        200: {"description": "Successfully authenticated"},
        401: {
            "description": "Invalid credentials, email not verified, or account deactivated"
        },
        429: {
            "description": "Too Many Requests - Rate limit exceeded",
            "content": {
                "application/json": {"example": {"error": "Rate limit exceeded"}}
            },
        },
        502: {"description": "Failed to connect to Supabase"},
    },
)
# @limiter.limit(RateLimits.AUTH_STRICT)
async def sign_in(
    request: Request,
    data: SignInRequest,
    service: AuthService = Depends(get_auth_service),
) -> SignInResponse:
    """
    Authenticate a user with Supabase and return session tokens.

    Args:
        request: The fastAPI request object.
        data: Payload containing credentials.
        service: The authentication service.

    Returns:
        A `SignInResponse` containing access and refresh tokens.

    Raises:
        UnauthorizedError: If credentials are invalid, the email is not
            verified, or the account is deactivated.
        BadGatewayError: If Supabase is unreachable.
    """
    return await service.sign_in(data)


@router.post(
    "/signout",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Sign out",
    description="""
    End the caller's session.

    - **Scope**: Revokes only the session this token belongs to; other devices
      stay signed in.
    - **Client**: Clear the stored access and refresh tokens after calling this.
    - **Idempotent**: Signing out an already-ended session still returns 200.
    """,
    responses={
        200: {"description": "Session ended"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        502: {
            "description": "Failed to connect to Supabase — the session was NOT revoked"
        },
    },
)
async def sign_out(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    current_user: AuthenticatedUser = Depends(require_auth),
    service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    """
    Revoke the caller's Supabase session.

    Args:
        request: The fastAPI request object.
        credentials: The bearer credentials the request authenticated with.
        current_user: The authenticated caller.
        service: The authentication service.

    Returns:
        A message confirming the session was ended.

    Raises:
        BadGatewayError: If Supabase is unreachable and the session could not
            be revoked.
    """
    # `credentials` alongside require_auth so the raw token is available to
    # revoke — require_auth resolves to an AuthenticatedUser and deliberately
    # does not carry the token. FastAPI caches a dependency per request, so
    # security_scheme is still only evaluated once.
    return await service.sign_out(credentials.credentials, current_user)


@router.post(
    "/resend-verification",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Resend verification email",
    description="""
    Resend the verification email for users who haven't verified their email yet.

    - **Use Case**: When verification link expires or email was not received.
    - **Response**: Always the same message, whether or not the address has an account.
    - **Rate Limit**: 3 requests per minute.
    """,
    responses={
        200: {"description": "Request accepted"},
        429: {
            "description": "Too Many Requests - Rate limit exceeded",
            "content": {
                "application/json": {"example": {"error": "Rate limit exceeded"}}
            },
        },
    },
)
# @limiter.limit(RateLimits.PASSWORD)
async def resend_verification(
    request: Request,
    data: ResendVerificationRequest,
    service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    """
    Resend the verification email to an unverified user.

    Args:
        request: The fastAPI request object.
        data: Payload containing the target email.
        service: The authentication service.

    Returns:
        A neutral message that does not reveal whether the address has an
        account.
    """
    return await service.resend_verification_email(data.email)
