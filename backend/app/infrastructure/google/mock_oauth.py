import uuid

from .base import GoogleAuthError, TokenSet


class MockGoogleOAuth:
    """Stand-in for Google sign-in. The consent screen is skipped: the
    authorization URL points straight at LexChain's own callback."""

    revoked: set[str] = set()

    def authorization_url(self, state: str) -> str:
        from app.core import settings

        return f"{settings.GOOGLE_REDIRECT_URI}?code=mock-code&state={state}"

    async def exchange_code(self, code: str) -> TokenSet:
        if code != "mock-code":
            raise GoogleAuthError("invalid_grant")
        return TokenSet(
            access_token=f"mock-access-{uuid.uuid4().hex}",
            expires_in=3600,
            refresh_token=f"mock-refresh-{uuid.uuid4().hex}",
        )

    async def refresh(self, refresh_token: str) -> TokenSet:
        if refresh_token in self.revoked:
            raise GoogleAuthError("invalid_grant")
        return TokenSet(access_token=f"mock-access-{uuid.uuid4().hex}", expires_in=3600)

    async def account_email(self, access_token: str) -> str:
        return "lawyer@gmail.test"

    async def revoke(self, token: str) -> None:
        self.revoked.add(token)
