from urllib.parse import urlencode

import httpx

from app.core import settings
from app.shared.errors.exceptions import BadGatewayError

from .base import SCOPES, GoogleAuthError, TokenSet

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class HttpGoogleOAuth:
    """Google's OAuth 2.0 web-server flow over plain HTTPS."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(timeout=20)

    def authorization_url(self, state: str) -> str:
        return f"{AUTH_URL}?" + urlencode(
            {
                "client_id": settings.GOOGLE_CLIENT_ID,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                # offline + consent: Google only issues a refresh token on a
                # fresh consent, and LexChain needs one to act later.
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
                "state": state,
            }
        )

    async def exchange_code(self, code: str) -> TokenSet:
        return await self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            }
        )

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await self._token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def account_email(self, access_token: str) -> str:
        response = await self.client.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if response.status_code == 401:
            raise GoogleAuthError("Google rejected the access token")
        if response.is_error:
            raise BadGatewayError("Could not read the Google account")
        return response.json()["email"]

    async def revoke(self, token: str) -> None:
        # Best effort: an already revoked token answers 400, which is the
        # state we want anyway.
        await self.client.post(REVOKE_URL, data={"token": token})

    async def _token(self, data: dict) -> TokenSet:
        response = await self.client.post(
            TOKEN_URL,
            data={
                **data,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
            },
        )
        if response.status_code in (400, 401):
            # invalid_grant: the lawyer revoked access or the code expired.
            raise GoogleAuthError(response.json().get("error", "invalid_grant"))
        if response.is_error:
            raise BadGatewayError("Google sign-in is unavailable, try again")
        body = response.json()
        return TokenSet(
            access_token=body["access_token"],
            expires_in=body.get("expires_in", 3600),
            refresh_token=body.get("refresh_token"),
        )
