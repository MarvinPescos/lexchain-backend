from fastapi import APIRouter, Depends, status, Request

# from app.infrastructure.database import get_db
# from app.infrastructure.cache import limiter, RateLimits
from .service import AuthService
from .dependencies import get_auth_service
from .schemas import (
    SignUpRequest,
    SignUpResponse,
    SignInRequest,
    SignInResponse,
    ResendVerificationRequest,
    MessageResponse,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post(
    "/signup",
    response_model=SignUpResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
    description="""
    Create a new user account with email, password, and username.
    
    - **Email Verification**: If enabled in Supabase, user must verify email before signing in.
    - **Username**: Must be 3-30 characters, alphanumeric and underscores only.
    - **Password**: Must be at least 8 characters with uppercase, lowercase, and digit.
    - **Rate Limit**: 5 requests per minute.
    """,
    responses={
        201: {"description": "User created successfully"},
        400: {"description": "Invalid input or user already exists"},
        429: {
            "description": "Too Many Requests - Rate limit exceeded",
            "content": {
                "application/json": {
                    "example": {"error": "Rate limit exceeded"}
                }
            }
        },
        502: {"description": "Failed to connect to Supabase"}
    }
)
# @limiter.limit(RateLimits.AUTH_STRICT)
async def sign_up(
    request: Request,
    data: SignUpRequest,
    service: AuthService = Depends(get_auth_service)
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
    """
    result = await service.sign_up(data)
    
    return SignUpResponse(
        message="Account created successfully. Please check your email to verify your account." 
                if result["requires_email_confirmation"] 
                else "Account created successfully.",
        user_id=result["user_id"],
        email=result["email"],
        requires_email_confirmation=result["requires_email_confirmation"],
    )


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
        401: {"description": "Invalid credentials or email not verified"},
        429: {
            "description": "Too Many Requests - Rate limit exceeded",
            "content": {
                "application/json": {
                    "example": {"error": "Rate limit exceeded"}
                }
            }
        }
    }
)
# @limiter.limit(RateLimits.AUTH_STRICT)
async def sign_in(
    request: Request,
    data: SignInRequest,
    # session: AsyncSession = Depends(get_db), #not been used? comment lang balikan ra tika     
    service: AuthService = Depends(get_auth_service)
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
        UnauthorizedError: If credentials are invalid or email is not verified.
    """
    result = await service.sign_in(data)
    
    return SignInResponse(
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
        expires_in=result["expires_in"],
        user=result["user"],
    )


@router.post(
    "/resend-verification",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Resend verification email",
    description="""
    Resend the verification email for users who haven't verified their email yet.
    
    - **Use Case**: When verification link expires or email was not received.
    - **Rate Limit**: 3 requests per minute.
    """,
    responses={
        200: {"description": "Verification email sent successfully"},
        400: {"description": "Could not send verification email"},
        429: {
            "description": "Too Many Requests - Rate limit exceeded",
            "content": {
                "application/json": {
                    "example": {"error": "Rate limit exceeded"}
                }
            }
        }
    }
)
# @limiter.limit(RateLimits.PASSWORD)
async def resend_verification(
    request: Request,
    data: ResendVerificationRequest,
    service: AuthService = Depends(get_auth_service)
) -> MessageResponse:
    """
    Resend the verification email to an unverified user.

    Args:
        request: The fastAPI request object.
        data: Payload containing the target email.
        service: The authentication service.

    Returns:
        A simple message response.

    Raises:
        BadRequestError: If the email cannot be sent.
    """
    result = service.resend_verification_email(data.email)
    return MessageResponse(message=result["message"])
