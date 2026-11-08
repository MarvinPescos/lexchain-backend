import uuid
from datetime import datetime

from app.shared.utils.validators import ValidatedName, ValidatedPassword
from pydantic import BaseModel, EmailStr, Field


# Removed 2026-09-14 19:55 PST — `phone_number` from SignUpRequest.
# It was validated, accepted, and then dropped on the floor: sign_up never
# passed it to sync_from_supabase and `users` has no column to hold it, so
# every caller sending one had it silently discarded. Collecting a personal
# phone number and storing it nowhere is the worst of both. ValidatedPhone
# stays in shared/utils/validators.py — re-add the field here together with a
# users.phone_number column and a migration when something actually reads it.
class SignUpRequest(BaseModel):
    email: EmailStr = Field(..., description="Valid email for this user")
    password: ValidatedPassword = Field(..., description="Valid Password")
    f_name: ValidatedName = Field(..., description="Valid first name")
    l_name: ValidatedName = Field(..., description="Valid last name")


class SignUpResponse(BaseModel):
    message: str = Field(..., description="Message containing what to do next.")
    user_id: uuid.UUID = Field(..., description="Contains the user id the response")
    email: EmailStr = Field(..., description="Email of this user/response")
    requires_email_confirmation: bool = Field(
        ..., description="session will be None until the user verifies their email"
    )


class SignInRequest(BaseModel):
    email: EmailStr = Field(..., description="Valid email for this user")
    # Changed 2026-09-14 19:55 PST — plain `str`, not ValidatedPassword.
    # Running the sign-up complexity policy on sign-in does two bad things: any
    # account whose password predates the policy can never log in again (422,
    # with no path to recover), and a malformed password answers differently
    # from a wrong one — 422 naming the rule that failed, versus 401 "Invalid
    # credentials" — which hands back the policy and a pre-auth oracle for
    # free. The password is only ever checked by Supabase; this layer has no
    # business inspecting it.
    password: str = Field(..., description="The user's password")


class SignInUser(BaseModel):
    """The signed-in user, as the client needs it."""

    id: uuid.UUID = Field(..., description="Local database user ID")
    email: EmailStr = Field(..., description="User's email address")
    role: str = Field(
        ...,
        description=(
            "lawyer — full authority (upload, review, finalize, admin panel); "
            "user — client, may only view documents they are a party to."
        ),
    )


class SignInResponse(BaseModel):
    access_token: str = Field(
        ..., description="JWT access token for API authentication"
    )
    refresh_token: str = Field(
        ..., description="Token used to obtain a new access token"
    )
    token_type: str = Field(default="bearer", description="Token type, always 'bearer'")
    expires_in: int = Field(..., description="Token expiration time in seconds")
    user: SignInUser = Field(..., description="The signed-in user")
    # Added 2026-11-08 — a password sign-in is only the first step.
    # The tokens above are aal1: every protected route answers 403 until a
    # code is verified at POST /auth/mfa/verify, which returns aal2 tokens in
    # this same shape with mfa_required=false.
    mfa_required: bool = Field(
        ...,
        description=(
            "True after a password sign-in: the tokens only reach the "
            "/auth/mfa endpoints until a code is verified."
        ),
    )
    mfa_factor_id: str | None = Field(
        default=None,
        description=(
            "The verified authenticator to send the code for. Null while "
            "mfa_required means the user has none yet and must enrol first "
            "(POST /auth/mfa/enroll)."
        ),
    )


class ResendVerificationRequest(BaseModel):
    email: EmailStr = Field(..., description="Email address to resend verification to")


class PasswordRecoveryRequest(BaseModel):
    email: EmailStr = Field(..., description="Address to send the reset link to")


class PasswordResetRequest(BaseModel):
    token_hash: str = Field(
        ...,
        min_length=1,
        description="The `token_hash` query parameter from the reset link",
    )
    new_password: ValidatedPassword = Field(
        ..., description="New password; same policy as sign-up"
    )


class MfaEnrollResponse(BaseModel):
    factor_id: str = Field(..., description="Pass to POST /auth/mfa/verify")
    qr_code: str = Field(
        ..., description="SVG data URI of the QR code to scan; usable as an <img> src"
    )
    secret: str = Field(
        ..., description="The TOTP secret, for typing in when the QR cannot be scanned"
    )
    uri: str = Field(..., description="The otpauth:// URI the QR code encodes")


class MfaVerifyRequest(BaseModel):
    factor_id: str = Field(..., min_length=1, description="The factor to verify")
    code: str = Field(
        ...,
        pattern=r"^\d{6}$",
        description="The 6-digit code from the authenticator app",
    )


class MfaFactor(BaseModel):
    id: str = Field(..., description="Factor ID")
    factor_type: str = Field(..., description="Always 'totp' here")
    status: str = Field(
        ..., description="verified, or unverified while enrolment is unconfirmed"
    )
    friendly_name: str | None = Field(default=None, description="Display name")
    created_at: datetime = Field(..., description="When the factor was enrolled")


class MfaFactorsResponse(BaseModel):
    factors: list[MfaFactor] = Field(..., description="The caller's factors")


class MessageResponse(BaseModel):
    message: str = Field(..., description="Response message")


# Moved 2026-09-14 19:55 PST — UserProfileResponse now lives in
# users/schemas.py. Nothing in this feature ever used it; it was imported from
# here by users/router.py and users/service.py, which made `users` depend on
# `auth` for its own response shape. Its unused `from_user` classmethod was
# dropped rather than moved.
