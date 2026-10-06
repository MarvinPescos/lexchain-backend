import uuid

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


class ResendVerificationRequest(BaseModel):
    email: EmailStr = Field(..., description="Email address to resend verification to")


class MessageResponse(BaseModel):
    message: str = Field(..., description="Response message")


# Moved 2026-09-14 19:55 PST — UserProfileResponse now lives in
# users/schemas.py. Nothing in this feature ever used it; it was imported from
# here by users/router.py and users/service.py, which made `users` depend on
# `auth` for its own response shape. Its unused `from_user` classmethod was
# dropped rather than moved.
