import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class CreateInvitationRequest(BaseModel):
    # Added 2026-09-07 00:01 PST — constrain the invitation input.
    # `role` was a free-form str whose description still advertised the retired
    # "admin" tier. Anything was accepted and written straight to the column, so
    # a typo like "lawer" produced an account matching neither role — silently
    # less privileged than a client, with no error anywhere in the invite/claim
    # chain. There are only two roles, so the type now says so and a bad value
    # is a 422 at the edge.
    # `email` was also a bare str while every other schema uses EmailStr.
    email: EmailStr = Field(..., description="Email to invite")
    role: Literal["lawyer", "user"] = Field(
        default="lawyer", description="lawyer | user"
    )


class InvitationResponse(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    status: str
    expires_at: datetime
    created_at: datetime
    magic_link: str | None = None

    class Config:
        from_attributes = True


class InvitationListResponse(BaseModel):
    invitations: list[InvitationResponse]


class AdminDashboardResponse(BaseModel):
    total_users: int
    total_lawyers: int
    total_documents: int
    total_processed: int
    total_failed: int
    total_on_chain: int
    pending_invitations: int


class AdminUserResponse(BaseModel):
    id: uuid.UUID
    email: str
    f_name: str
    l_name: str
    role: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class AdminUserListResponse(BaseModel):
    users: list[AdminUserResponse]
    total: int


# Added 2026-09-12 16:59 PST — the account off-switch had no switch.
# get_current_user and sign_in both refuse inactive accounts, but nothing in
# the codebase ever set is_active to False: it was written once at INSERT and
# only ever read, so disabling someone meant editing the database by hand.
class SetUserActiveRequest(BaseModel):
    is_active: bool = Field(
        ..., description="False deactivates the account, True restores it"
    )
