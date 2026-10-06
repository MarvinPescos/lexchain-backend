from typing import Protocol

from pydantic import BaseModel

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


class GoogleOAuth(Protocol):
    def authorization_url(self, state: str) -> str: ...

    async def exchange_code(self, code: str) -> TokenSet: ...

    async def refresh(self, refresh_token: str) -> TokenSet: ...

    async def account_email(self, access_token: str) -> str: ...

    async def revoke(self, token: str) -> None: ...


class GoogleAuthError(Exception):
    """Google refused the credentials — the lawyer has to reconnect."""
