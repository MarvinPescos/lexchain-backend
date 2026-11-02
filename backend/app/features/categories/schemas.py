import uuid

from pydantic import BaseModel, ConfigDict, Field


class CategoryWriteRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CategoryResponse(BaseModel):
    id: uuid.UUID
    name: str
    active: bool

    model_config = ConfigDict(from_attributes=True)
