from typing import Protocol

from pydantic import BaseModel

from app.shared.errors.exceptions import ConflictError

# drive.file: LexChain sees only the files it created or the lawyer picked
# for it — never the rest of their Drive. openid + email identify which
# Google account was connected.
SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/drive.file",
)


class TokenSet(BaseModel):
    access_token: str
    expires_in: int
    # Only returned on the first consent (access_type=offline); a refresh
    # returns none and the stored one stays valid.
    refresh_token: str | None = None
    # Space-separated scopes actually granted. Google's consent screen lets the
    # user untick a permission, so this can be less than what was requested.
    scope: str = ""

    def granted(self, scope: str) -> bool:
        return scope in self.scope.split()


class GoogleOAuth(Protocol):
    def authorization_url(self, state: str) -> str: ...

    async def exchange_code(self, code: str) -> TokenSet: ...

    async def refresh(self, refresh_token: str) -> TokenSet: ...

    async def account_email(self, access_token: str) -> str: ...

    async def revoke(self, token: str) -> None: ...


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"


class GoogleAuthError(ConflictError):
    """Google refused the credentials (revoked, expired, or a permission
    missing) — the lawyer has to reconnect. Answers 409, never a 500."""

    def __init__(self, reason: str = "") -> None:
        self.reason = reason
        super().__init__(
            "Google access was revoked or is missing a permission. Reconnect Google.",
            details={"reason": "GOOGLE_RECONNECT_REQUIRED"},
        )
