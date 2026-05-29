import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class CreateInvitationRequest(BaseModel):
    email: str = Field(..., description="Email to invite")
    role: str = Field(default="lawyer", description="lawyer | admin")


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
