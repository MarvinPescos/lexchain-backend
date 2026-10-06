"""Connecting a lawyer's Google account."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.features.google.models import GoogleConnection
from app.shared.errors.exceptions import BadRequestError, ConflictError
from tests.conftest import connect_google


async def stored(session, user):
    return (
        await session.execute(select(GoogleConnection).where(GoogleConnection.user_id == user.id))
    ).scalar_one_or_none()


async def test_connect_stores_tokens_encrypted(google, session, lawyer):
    await connect_google(google, lawyer)

    status = await google.status(lawyer.id)
    assert (status.connected, status.google_email) == (True, "lawyer@gmail.test")

    row = await stored(session, lawyer)
    assert not row.refresh_token.startswith("mock-refresh")
    token = await google.access_token(lawyer.id)
    assert token.startswith("mock-access")


async def test_expired_access_token_is_refreshed(google, session, lawyer):
    await connect_google(google, lawyer)
    first = await google.access_token(lawyer.id)

    row = await stored(session, lawyer)
    row.access_token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.flush()

    assert await google.access_token(lawyer.id) != first


async def test_tampered_state_is_refused(google, lawyer):
    google.start(lawyer.id)
    with pytest.raises(BadRequestError, match="expired"):
        await google.complete("mock-code", "not-a-real-state")


async def test_disconnect_revokes_and_forgets(google, session, lawyer):
    await connect_google(google, lawyer)
    await google.disconnect(lawyer.id)

    assert await stored(session, lawyer) is None
    with pytest.raises(ConflictError):
        await google.access_token(lawyer.id)


async def test_access_revoked_at_google_drops_the_connection(google, session, lawyer):
    await connect_google(google, lawyer)
    row = await stored(session, lawyer)
    row.access_token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.flush()
    from cryptography.fernet import Fernet  # noqa: F401 — refresh token is encrypted

    google.oauth.refresh = _revoked
    with pytest.raises(ConflictError) as refused:
        await google.access_token(lawyer.id)
    assert refused.value.details == {"reason": "GOOGLE_NOT_CONNECTED"}
    assert await stored(session, lawyer) is None


async def _revoked(refresh_token):
    from app.infrastructure.google import GoogleAuthError

    raise GoogleAuthError("invalid_grant")
