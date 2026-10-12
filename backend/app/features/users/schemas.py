import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.shared.utils.validators import ValidatedName


class UserSearchResponse(BaseModel):
    user_id: uuid.UUID = Field(..., description="")
    email: str = Field(..., description="")
    f_name: str = Field(..., description="")
    l_name: str = Field(..., description="")


# Moved 2026-09-14 19:55 PST — was in auth/schemas.py.
# It is the response shape of GET /users/, read by users/router.py and
# users/service.py and by nothing in the auth feature, so it lived one import
# away from every module that used it and gave `users` a dependency on `auth`
# for its own response.
#
# `from_user` did not come along: nothing called it, and it built the response
# without `avatar`, so it would have reported "icon1" for every user no matter
# what they had set.
class UserProfileResponse(BaseModel):
    email: str = Field(..., description="User's email address")
    f_name: ValidatedName = Field(..., description="Valid first name")
    l_name: ValidatedName = Field(..., description="Valid last name")
    avatar: str = Field(default="icon1", description="User Avatar")
    role: str = Field(..., description="User role")

    @field_validator("avatar", mode="before")
    @classmethod
    def _fallback_avatar(cls, value):
        """Fall back to the default when the column is NULL.

        A Field default only applies when the key is absent — passing an
        explicit None still fails validation, so a NULL avatar would 500 the
        profile endpoint.

        Updated 2026-09-14 19:55 PST: the 2026-09-12 note here said this could
        go once users.avatar was non-nullable. The *model* says NOT NULL with a
        server_default, but no migration was ever generated for that change —
        the live column is still the initial migration's `avatar VARCHAR(500)
        NULL`, so NULLs are still reachable and this guard is still load-
        bearing. Delete it after the migration lands and the rows are
        backfilled:
            UPDATE users SET avatar = 'icon1' WHERE avatar IS NULL;
        """
        return value or "icon1"

    # Changed 2026-09-14 19:55 PST — ConfigDict, not `class Config`.
    # Pydantic deprecated class-based config in 2.0 and drops it in 3.0; it
    # raises PydanticDeprecatedSince20 on import today. Same reasoning as
    # infrastructure/security/dependencies.py.
    model_config = ConfigDict(from_attributes=True)
