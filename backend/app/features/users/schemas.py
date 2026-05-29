import uuid

from pydantic import BaseModel, Field


class UserSearchResponse(BaseModel):
    user_id: uuid.UUID = Field(..., description="")
    email: str = Field(..., description="")
    f_name: str = Field(..., description="")
    l_name: str = Field(..., description="")
