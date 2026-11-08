from app.infrastructure.security.dependencies import (
    AuthenticatedUser,
    get_first_factor_user,
    require_auth,
    security_scheme,
)
from fastapi import APIRouter, Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials

from app.infrastructure.cache import RateLimits, limiter
from .dependencies import get_auth_service
from .schemas import (
    MessageResponse,
    MfaEnrollResponse,
    MfaFactorsResponse,
    MfaVerifyRequest,
    PasswordRecoveryRequest,
    PasswordResetRequest,
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
@limiter.limit(RateLimits.AUTH_STRICT)
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
    - **MFA**: The tokens are aal1 and only reach the `/auth/mfa` endpoints and
      sign-out (`mfa_required` is always true). Send the authenticator code to
      `POST /auth/mfa/verify` with `mfa_factor_id`; if that is null, enrol first
      at `POST /auth/mfa/enroll`.
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
@limiter.limit(RateLimits.AUTH_STRICT)
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
    # aal1 too: someone stopped at the code prompt must still be able to
    # end the half-open session.
    current_user: AuthenticatedUser = Depends(get_first_factor_user),
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
@limiter.limit(RateLimits.PASSWORD)
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


@router.post(
    "/password-recovery",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Request a password reset email",
    description="""
    Email a password-reset link to the address, if it has an account.

    - **Response**: Always the same message, whether or not the address has an account.
    - **Link**: Opens the frontend reset page with `?token_hash=...&type=recovery`.
      Pass `token_hash` to `POST /auth/password-reset`.
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
@limiter.limit(RateLimits.PASSWORD)
async def request_password_recovery(
    request: Request,
    data: PasswordRecoveryRequest,
    service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    """
    Send a password-reset email.

    Args:
        request: The fastAPI request object.
        data: Payload containing the target email.
        service: The authentication service.

    Returns:
        A neutral message that does not reveal whether the address has an
        account.
    """
    return await service.request_password_recovery(data.email)


@router.post(
    "/password-reset",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Set a new password from a reset link",
    description="""
    Set a new password using the `token_hash` from a password-reset link.

    - **Single use**: A link works once and expires per the Supabase email OTP
      expiry (default 1 hour). A 400 or 502 spends the link — request a new one.
      A 422 (password policy) is rejected before the link is checked.
    - **Sessions**: Every refresh token for the account is revoked; the user signs
      in again. Access tokens already issued stay valid until they expire.
    - **Password**: Same policy as sign-up (8+ characters, upper, lower, digit).
    - **Rate Limit**: 5 requests per minute.
    """,
    responses={
        200: {"description": "Password updated; all sessions revoked"},
        400: {
            "description": "Link invalid, expired or already used, or Supabase rejected the password"
        },
        422: {"description": "New password does not meet the policy"},
        429: {
            "description": "Too Many Requests - Rate limit exceeded",
            "content": {
                "application/json": {"example": {"error": "Rate limit exceeded"}}
            },
        },
        502: {"description": "Failed to connect to Supabase"},
    },
)
@limiter.limit(RateLimits.AUTH_STRICT)
async def reset_password(
    request: Request,
    data: PasswordResetRequest,
    service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    """
    Set a new password from a recovery link.

    Args:
        request: The fastAPI request object.
        data: Payload containing the link's token hash and the new password.
        service: The authentication service.

    Returns:
        A message telling the user to sign in with the new password.

    Raises:
        BadRequestError: If the link is invalid, expired or already used.
        BadGatewayError: If Supabase is unreachable.
    """
    return await service.reset_password(data.token_hash, data.new_password)


# === Multi-factor authentication ===
# These take the raw token alongside the user for the same reason sign_out
# does: GoTrue is called as the user, with their own JWT.


@router.post(
    "/mfa/enroll",
    response_model=MfaEnrollResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start authenticator-app enrolment",
    description="""
    Create a TOTP factor and return the QR code to scan.

    - **Session**: The aal1 token from sign-in is enough while the user has no
      verified authenticator. Adding a second one needs an aal2 session.
    - **Confirm**: The factor stays unverified until a code from it is sent to
      `POST /auth/mfa/verify`, which also returns the aal2 tokens.
    - **Retry**: A previous unconfirmed enrolment is discarded.
    - **Secret**: `secret` and `qr_code` are shown once; never log or store them.
    """,
    responses={
        201: {"description": "Factor created, awaiting its first code"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "An authenticator is already enrolled; verify it first"},
        502: {"description": "Failed to connect to Supabase"},
    },
)
async def enroll_mfa(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    current_user: AuthenticatedUser = Depends(get_first_factor_user),
    service: AuthService = Depends(get_auth_service),
) -> MfaEnrollResponse:
    """Start TOTP enrolment for the caller."""
    return await service.enroll_mfa(credentials.credentials, current_user)


@router.post(
    "/mfa/verify",
    response_model=SignInResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify an authenticator code",
    description="""
    Check a 6-digit code and return aal2 tokens that reach every route.

    - **Use**: After every sign-in, and once to confirm a new enrolment.
    - **Tokens**: Replace the aal1 tokens with the ones returned here.
    - **Rate Limit**: 5 requests per minute.
    """,
    responses={
        200: {"description": "Code accepted; aal2 session issued"},
        401: {"description": "Invalid code, or missing/invalid bearer token"},
        422: {"description": "Code is not 6 digits"},
        429: {
            "description": "Too Many Requests - Rate limit exceeded",
            "content": {
                "application/json": {"example": {"error": "Rate limit exceeded"}}
            },
        },
        502: {"description": "Failed to connect to Supabase"},
    },
)
@limiter.limit(RateLimits.AUTH_STRICT)
async def verify_mfa(
    request: Request,
    data: MfaVerifyRequest,
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    current_user: AuthenticatedUser = Depends(get_first_factor_user),
    service: AuthService = Depends(get_auth_service),
) -> SignInResponse:
    """Verify a TOTP code and upgrade the caller's session to aal2."""
    return await service.verify_mfa(
        credentials.credentials, current_user, data.factor_id, data.code
    )


@router.get(
    "/mfa/factors",
    response_model=MfaFactorsResponse,
    status_code=status.HTTP_200_OK,
    summary="List authenticators",
    description="""
    The caller's TOTP factors, verified or not. An aal1 session is enough.
    """,
    responses={
        200: {"description": "The caller's factors"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        502: {"description": "Failed to connect to Supabase"},
    },
)
async def list_mfa_factors(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    current_user: AuthenticatedUser = Depends(get_first_factor_user),
    service: AuthService = Depends(get_auth_service),
) -> MfaFactorsResponse:
    """List the caller's factors."""
    return await service.list_mfa_factors(credentials.credentials, current_user)


@router.delete(
    "/mfa/factors/{factor_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Remove an authenticator",
    description="""
    Remove one of the caller's factors.

    - **Session**: Requires aal2 — a password alone cannot remove MFA.
    - **Last factor**: MFA stays mandatory; the next sign-in goes through
      enrolment again.
    """,
    responses={
        200: {"description": "Factor removed"},
        400: {"description": "The factor could not be removed"},
        401: {"description": "Not authenticated — missing or invalid bearer token"},
        403: {"description": "Session has not passed MFA"},
        502: {"description": "Failed to connect to Supabase"},
    },
)
async def remove_mfa_factor(
    factor_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    current_user: AuthenticatedUser = Depends(require_auth),
    service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    """Remove one of the caller's factors."""
    return await service.remove_mfa_factor(
        credentials.credentials, current_user, factor_id
    )
