"""A lawyer's Google account connection.

Connecting runs Google's OAuth web flow once; afterwards `access_token`
hands out a fresh token whenever LexChain acts in the lawyer's Drive,
refreshing it with the stored refresh token as needed.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings
from app.infrastructure.google import DRIVE_SCOPE, GoogleAuthError, GoogleOAuth
from app.infrastructure.observability.logging_setup import log
from app.shared.errors.exceptions import BadRequestError, ConflictError

from .repository import GoogleConnectionRepository
from .schemas import (
    GoogleConnectionStatus,
    GoogleConnectResponse,
    GooglePickerToken,
)

# The OAuth state is only valid while the lawyer is on Google's consent screen.
STATE_TTL_SECONDS = 600
# Refresh a little early so a token never expires mid-request.
EXPIRY_MARGIN = timedelta(seconds=60)

# Without a configured key (mock mode) a per-process key is fine: there are
# no real tokens to keep across restarts.
_DEV_KEY = Fernet.generate_key()


def _fernet() -> Fernet:
    if settings.GOOGLE_TOKEN_KEY:
        return Fernet(settings.GOOGLE_TOKEN_KEY.encode())
    if settings.USE_REAL_GOOGLE:
        raise RuntimeError("GOOGLE_TOKEN_KEY must be set when USE_REAL_GOOGLE is on")
    return Fernet(_DEV_KEY)


def not_connected_error() -> ConflictError:
    return ConflictError(
        "Connect your Google account first.",
        details={"reason": "GOOGLE_NOT_CONNECTED"},
    )


class GoogleConnectionService:
    def __init__(self, session: AsyncSession, oauth: GoogleOAuth) -> None:
        self.session = session
        self.oauth = oauth
        self.repo = GoogleConnectionRepository(session)

    # === Connect ===

    def start(self, user_id: uuid.UUID) -> GoogleConnectResponse:
        """The Google consent URL. The state carries the user, encrypted, so the
        callback — which has no LexChain token — knows whose account it is."""
        state = _fernet().encrypt(
            json.dumps({"user_id": str(user_id), "nonce": uuid.uuid4().hex}).encode()
        )
        return GoogleConnectResponse(
            authorization_url=self.oauth.authorization_url(state.decode())
        )

    async def complete(self, code: str, state: str) -> uuid.UUID:
        """Finish the OAuth flow from Google's redirect. Returns the user id."""
        try:
            payload = json.loads(_fernet().decrypt(state.encode(), ttl=STATE_TTL_SECONDS))
        except (InvalidToken, ValueError):
            raise BadRequestError("The Google sign-in link expired. Connect again.")
        user_id = uuid.UUID(payload["user_id"])

        try:
            tokens = await self.oauth.exchange_code(code)
        except GoogleAuthError:
            raise BadRequestError("Google sign-in was not completed. Connect again.")
        if not tokens.refresh_token:
            raise BadRequestError("Google did not grant offline access. Connect again.")
        if not tokens.granted(DRIVE_SCOPE):
            # The consent screen shows Drive access as an unticked checkbox;
            # without it every draft call would fail later. Store nothing.
            await self.oauth.revoke(tokens.refresh_token)
            raise BadRequestError(
                "LexChain needs access to the Google Docs it creates. Connect "
                "again and tick the Google Drive permission."
            )
        email = await self.oauth.account_email(tokens.access_token)

        fernet = _fernet()
        values = dict(
            google_email=email,
            refresh_token=fernet.encrypt(tokens.refresh_token.encode()).decode(),
            access_token=fernet.encrypt(tokens.access_token.encode()).decode(),
            access_token_expires_at=self._expiry(tokens.expires_in),
        )
        connection = await self.repo.get_for_user(user_id)
        if connection:
            await self.repo.update(connection, **values)
        else:
            await self.repo.create(user_id=user_id, **values)
        await self.session.commit()

        log.info("google.connect", user_id=str(user_id), google_email=email)
        return user_id

    async def status(self, user_id: uuid.UUID) -> GoogleConnectionStatus:
        connection = await self.repo.get_for_user(user_id)
        if connection is None:
            return GoogleConnectionStatus(connected=False)
        return GoogleConnectionStatus(
            connected=True,
            google_email=connection.google_email,
            connected_at=connection.created_at,
        )

    async def disconnect(self, user_id: uuid.UUID) -> None:
        """Revoke LexChain's access at Google and forget the tokens.

        Drafts already created stay in the lawyer's Drive; LexChain just can
        no longer share or lock them until the lawyer reconnects.
        """
        connection = await self.repo.get_for_user(user_id)
        if connection is None:
            return
        try:
            await self.oauth.revoke(_fernet().decrypt(connection.refresh_token.encode()).decode())
        except Exception as e:
            log.warning("google.revoke.failed", user_id=str(user_id), error=str(e))
        await self.repo.delete(connection)
        await self.session.commit()
        log.info("google.disconnect", user_id=str(user_id))

    # === Use ===

    async def access_token(self, user_id: uuid.UUID) -> str:
        """A valid access token for acting in this lawyer's Drive."""
        connection = await self.repo.get_for_user(user_id)
        if connection is None:
            raise not_connected_error()

        fernet = _fernet()
        now = datetime.now(timezone.utc)
        if (
            connection.access_token
            and connection.access_token_expires_at
            and connection.access_token_expires_at - EXPIRY_MARGIN > now
        ):
            return fernet.decrypt(connection.access_token.encode()).decode()

        try:
            tokens = await self.oauth.refresh(
                fernet.decrypt(connection.refresh_token.encode()).decode()
            )
        except GoogleAuthError:
            # Revoked from the Google side: the connection is dead.
            log.warning("google.refresh.revoked", user_id=str(user_id))
            await self.repo.delete(connection)
            await self.session.commit()
            raise not_connected_error()

        await self.repo.update(
            connection,
            access_token=fernet.encrypt(tokens.access_token.encode()).decode(),
            access_token_expires_at=self._expiry(tokens.expires_in),
        )
        await self.session.commit()
        return tokens.access_token

    async def picker_token(self, user_id: uuid.UUID) -> GooglePickerToken:
        """A short-lived token for the frontend's Google Picker."""
        token = await self.access_token(user_id)
        connection = await self.repo.get_for_user(user_id)
        return GooglePickerToken(
            access_token=token, expires_at=connection.access_token_expires_at
        )

    def _expiry(self, expires_in: int) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=expires_in)
