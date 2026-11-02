from types import SimpleNamespace

import httpx
import pytest
from supabase import AuthApiError

from app.core import settings
from app.features.auth import service as auth_service
from app.features.auth.dependencies import get_auth_service
from app.features.auth.schemas import MessageResponse
from app.features.auth.service import AuthService
from app.infrastructure.cache import limiter
from app.main import app
from app.shared.errors.exceptions import BadGatewayError, BadRequestError


class FakeGoTrue:
    """Records what AuthService asked GoTrue for; fails on demand."""

    def __init__(self, fail_with: dict[str, Exception] | None = None):
        self.calls: list[tuple] = []
        self.fail_with = fail_with or {}
        self.consumed: set[str] = set()
        self.admin = SimpleNamespace(
            update_user_by_id=self._record("update_user_by_id"),
            sign_out=self._record("sign_out"),
        )

    def _record(self, name):
        def call(*args):
            self.calls.append((name, *args))
            if name in self.fail_with:
                raise self.fail_with[name]

        return call

    def reset_password_for_email(self, email, options):
        self._record("reset_password_for_email")(email, options)

    def verify_otp(self, params):
        self.calls.append(("verify_otp", params))
        if "verify_otp" in self.fail_with:
            raise self.fail_with["verify_otp"]
        if params["token_hash"] in self.consumed:
            raise AuthApiError("Email link is invalid or has expired", 403, None)
        self.consumed.add(params["token_hash"])
        return SimpleNamespace(
            user=SimpleNamespace(id="sb-user-1"),
            session=SimpleNamespace(access_token="recovery-jwt"),
        )

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def gotrue(monkeypatch):
    fake = FakeGoTrue()
    client = SimpleNamespace(auth=fake)
    monkeypatch.setattr(auth_service, "build_supabase_auth_client", lambda: client)
    monkeypatch.setattr(
        auth_service, "get_supabase_admin", lambda: SimpleNamespace(auth=fake)
    )
    return fake


@pytest.fixture
def service(gotrue):
    return AuthService(session=None)


# === Request a recovery email ===


async def test_recovery_email_redirects_to_the_configured_page(service, gotrue):
    await service.request_password_recovery("client@firm.ph")

    assert gotrue.calls == [
        (
            "reset_password_for_email",
            "client@firm.ph",
            {"redirect_to": settings.PASSWORD_RESET_REDIRECT},
        )
    ]


@pytest.mark.parametrize(
    "failure",
    [AuthApiError("User not found", 400, None), ConnectionError("gotrue down")],
)
async def test_recovery_reply_is_the_same_whatever_happens(service, gotrue, failure):
    sent = await service.request_password_recovery("client@firm.ph")
    gotrue.fail_with["reset_password_for_email"] = failure
    failed = await service.request_password_recovery("nobody@firm.ph")

    assert sent == failed


# === Set the new password ===


async def test_reset_sets_the_password_and_ends_every_session(service, gotrue):
    result = await service.reset_password("hash-1", "N3wPassword!")

    assert gotrue.calls == [
        ("verify_otp", {"type": "recovery", "token_hash": "hash-1"}),
        ("update_user_by_id", "sb-user-1", {"password": "N3wPassword!"}),
        ("sign_out", "recovery-jwt", "global"),
    ]
    assert isinstance(result, MessageResponse)


async def test_a_reset_link_works_once(service, gotrue):
    await service.reset_password("hash-1", "N3wPassword!")

    with pytest.raises(BadRequestError):
        await service.reset_password("hash-1", "An0therPassword!")


async def test_an_expired_link_is_refused_without_touching_the_password(
    service, gotrue
):
    gotrue.fail_with["verify_otp"] = AuthApiError("Token has expired", 403, None)

    with pytest.raises(BadRequestError):
        await service.reset_password("hash-1", "N3wPassword!")
    assert "update_user_by_id" not in gotrue.names()


async def test_reset_reports_gotrue_being_down(service, gotrue):
    gotrue.fail_with["verify_otp"] = ConnectionError("gotrue down")

    with pytest.raises(BadGatewayError):
        await service.reset_password("hash-1", "N3wPassword!")


async def test_a_rejected_password_says_so_and_closes_the_recovery_session(
    service, gotrue
):
    gotrue.fail_with["update_user_by_id"] = AuthApiError("weak password", 422, None)

    with pytest.raises(BadRequestError) as refused:
        await service.reset_password("hash-1", "N3wPassword!")

    assert "password could not be set" in str(refused.value.message)
    assert gotrue.calls[-1] == ("sign_out", "recovery-jwt", "local")


async def test_reset_still_succeeds_if_revoking_old_sessions_fails(service, gotrue):
    gotrue.fail_with["sign_out"] = ConnectionError("gotrue down")

    result = await service.reset_password("hash-1", "N3wPassword!")

    assert isinstance(result, MessageResponse)


# === HTTP contract ===


class FakeAuth:
    def __init__(self):
        self.reset_calls = []

    async def request_password_recovery(self, email):
        return MessageResponse(message="neutral")

    async def reset_password(self, token_hash, new_password):
        self.reset_calls.append((token_hash, new_password))
        return MessageResponse(message="done")


@pytest.fixture
async def http():
    limiter.reset()
    fake = FakeAuth()
    app.dependency_overrides[get_auth_service] = lambda: fake
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            client.fake = fake
            yield client
    finally:
        app.dependency_overrides.clear()
        limiter.reset()


async def test_recovery_endpoint_is_throttled(http):
    codes = [
        (
            await http.post("/auth/password-recovery", json={"email": "client@firm.ph"})
        ).status_code
        for _ in range(4)
    ]

    assert codes == [200, 200, 200, 429]


async def test_reset_endpoint_applies_the_signup_password_policy(http):
    response = await http.post(
        "/auth/password-reset", json={"token_hash": "hash-1", "new_password": "weak"}
    )

    assert response.status_code == 422
    assert http.fake.reset_calls == []


async def test_reset_endpoint_passes_the_link_token_through(http):
    response = await http.post(
        "/auth/password-reset",
        json={"token_hash": "hash-1", "new_password": "N3wPassword!"},
    )

    assert response.status_code == 200
    assert http.fake.reset_calls == [("hash-1", "N3wPassword!")]


async def test_reset_endpoint_is_throttled(http):
    body = {"token_hash": "hash-1", "new_password": "N3wPassword!"}
    codes = [
        (await http.post("/auth/password-reset", json=body)).status_code
        for _ in range(6)
    ]

    assert codes == [200] * 5 + [429]
