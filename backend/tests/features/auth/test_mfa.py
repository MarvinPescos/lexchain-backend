from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from supabase import AuthApiError

from app.features.auth import service as auth_service
from app.features.auth.dependencies import get_auth_service
from app.features.auth.schemas import (
    MessageResponse,
    MfaFactorsResponse,
    SignInRequest,
    SignInResponse,
    SignInUser,
)
from app.features.auth.service import AuthService
from app.infrastructure.cache import limiter
from app.infrastructure.database import get_db
from app.infrastructure.security import dependencies as security
from app.main import app
from app.shared.errors.exceptions import (
    BadGatewayError,
    ForbiddenError,
    UnauthorizedError,
)
from tests.conftest import as_auth

GOOD_CODE = "123456"
NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)


def factor(id, status="verified"):
    return SimpleNamespace(
        id=id,
        factor_type="totp",
        status=status,
        friendly_name=None,
        created_at=NOW,
    )


class FakeMfa:
    """Stands in for SupabaseMfa: GoTrue's factor endpoints for one user."""

    def __init__(self, factors=None):
        self.factors = list(factors or [])
        self.calls: list[tuple] = []
        self.fail_with: dict[str, Exception] = {}

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if name in self.fail_with:
            raise self.fail_with[name]

    def list_factors(self, jwt):
        self._call("list_factors", jwt)
        return list(self.factors)

    def enroll_totp(self, jwt):
        self._call("enroll_totp", jwt)
        self.factors.append(factor("new-factor", status="unverified"))
        return SimpleNamespace(
            id="new-factor",
            totp=SimpleNamespace(
                qr_code="data:image/svg+xml;utf-8,<svg/>",
                secret="SECRET",
                uri="otpauth://totp/LexChain:x",
            ),
        )

    def challenge_and_verify(self, jwt, factor_id, code):
        self._call("challenge_and_verify", jwt, factor_id, code)
        if code != GOOD_CODE:
            raise AuthApiError("Invalid TOTP code entered", 422, None)
        return SimpleNamespace(
            access_token="aal2-jwt", refresh_token="aal2-refresh", expires_in=3600
        )

    def unenroll(self, jwt, factor_id):
        self._call("unenroll", jwt, factor_id)
        self.factors = [f for f in self.factors if f.id != factor_id]


@pytest.fixture
async def lawyer(lawyer, session):
    # SignInUser.email is an EmailStr, which refuses conftest's .local domain.
    lawyer.email = "lawyer@firm.ph"
    await session.flush()
    return lawyer


@pytest.fixture
def mfa():
    return FakeMfa()


@pytest.fixture
def service(mfa, monkeypatch, session):
    monkeypatch.setattr(auth_service, "get_supabase_admin", lambda: None)
    svc = AuthService(session=session)
    svc.mfa = mfa
    return svc


def signed_in(user, aal="aal1"):
    return as_auth(user).model_copy(update={"aal": aal})


# === Sign-in hands back an aal1 session ===


@pytest.fixture
def gotrue_sign_in(monkeypatch, lawyer):
    factors: list = []

    def sign_in_with_password(credentials):
        return SimpleNamespace(
            user=SimpleNamespace(
                id=lawyer.supabase_user_id,
                email=lawyer.email,
                email_confirmed_at=NOW,
                factors=factors,
            ),
            session=SimpleNamespace(
                access_token="aal1-jwt", refresh_token="aal1-refresh", expires_in=3600
            ),
        )

    client = SimpleNamespace(
        auth=SimpleNamespace(sign_in_with_password=sign_in_with_password)
    )
    monkeypatch.setattr(auth_service, "build_supabase_auth_client", lambda: client)
    return factors


async def test_sign_in_asks_for_the_code_of_the_enrolled_authenticator(
    service, gotrue_sign_in, lawyer
):
    gotrue_sign_in += [factor("stale", status="unverified"), factor("phone-app")]

    result = await service.sign_in(SignInRequest(email=lawyer.email, password="pw"))

    assert (result.mfa_required, result.mfa_factor_id) == (True, "phone-app")


async def test_sign_in_without_an_authenticator_sends_the_user_to_enrol(
    service, gotrue_sign_in, lawyer
):
    result = await service.sign_in(SignInRequest(email=lawyer.email, password="pw"))

    assert (result.mfa_required, result.mfa_factor_id) == (True, None)


# === Verify ===


async def test_the_right_code_returns_an_aal2_session(service, mfa, lawyer):
    result = await service.verify_mfa(
        "aal1-jwt", signed_in(lawyer), "phone-app", GOOD_CODE
    )

    assert (result.access_token, result.refresh_token) == ("aal2-jwt", "aal2-refresh")
    assert result.mfa_required is False
    assert result.user.id == lawyer.id
    assert mfa.calls == [("challenge_and_verify", "aal1-jwt", "phone-app", GOOD_CODE)]


async def test_a_wrong_code_is_refused(service, lawyer):
    with pytest.raises(UnauthorizedError):
        await service.verify_mfa("aal1-jwt", signed_in(lawyer), "phone-app", "000000")


async def test_verify_reports_gotrue_being_down(service, mfa, lawyer):
    mfa.fail_with["challenge_and_verify"] = ConnectionError("gotrue down")

    with pytest.raises(BadGatewayError):
        await service.verify_mfa("aal1-jwt", signed_in(lawyer), "phone-app", GOOD_CODE)


# === Enrol ===


async def test_enrolment_returns_the_qr_code(service, mfa, lawyer):
    result = await service.enroll_mfa("aal1-jwt", signed_in(lawyer))

    assert result.factor_id == "new-factor"
    assert result.qr_code.startswith("data:image/svg+xml")
    assert result.secret == "SECRET"


async def test_an_abandoned_enrolment_is_cleared_before_a_new_one(
    service, mfa, lawyer
):
    mfa.factors = [factor("abandoned", status="unverified")]

    await service.enroll_mfa("aal1-jwt", signed_in(lawyer))

    assert ("unenroll", "aal1-jwt", "abandoned") in mfa.calls
    assert [f.id for f in mfa.factors] == ["new-factor"]


async def test_a_password_alone_cannot_add_a_second_authenticator(
    service, mfa, lawyer
):
    mfa.factors = [factor("phone-app")]

    with pytest.raises(ForbiddenError):
        await service.enroll_mfa("aal1-jwt", signed_in(lawyer))
    assert "enroll_totp" not in [c[0] for c in mfa.calls]


async def test_an_aal2_session_can_add_a_second_authenticator(service, mfa, lawyer):
    mfa.factors = [factor("phone-app")]

    result = await service.enroll_mfa("aal2-jwt", signed_in(lawyer, "aal2"))

    assert result.factor_id == "new-factor"


# === List and remove ===


async def test_factors_are_listed(service, mfa, lawyer):
    mfa.factors = [factor("phone-app")]

    result = await service.list_mfa_factors("aal1-jwt", signed_in(lawyer))

    assert [(f.id, f.status) for f in result.factors] == [("phone-app", "verified")]


async def test_a_factor_is_removed(service, mfa, lawyer):
    mfa.factors = [factor("phone-app")]

    await service.remove_mfa_factor("aal2-jwt", signed_in(lawyer, "aal2"), "phone-app")

    assert mfa.factors == []


# === Enforcement on every protected route ===


@pytest.fixture
async def http(session, monkeypatch):
    """A client whose bearer token decodes to {"sub": ..., "aal": token}."""
    claims: dict[str, dict] = {}
    monkeypatch.setattr(security, "verify_jwt_token", lambda token: claims[token])

    async def same_session():
        yield session

    limiter.reset()
    app.dependency_overrides[get_db] = same_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            client.claims = claims
            yield client
    finally:
        app.dependency_overrides.clear()
        limiter.reset()


def bearer(http, user, aal):
    token = f"token-{aal}"
    claims = {"sub": user.supabase_user_id, "aud": "authenticated"}
    if aal:
        claims["aal"] = aal
    http.claims[token] = claims
    return {"Authorization": f"Bearer {token}"}


async def test_a_password_only_session_is_refused_on_protected_routes(http, lawyer):
    r = await http.get("/users/", headers=bearer(http, lawyer, "aal1"))

    assert r.status_code == 403
    assert r.json()["details"] == {"mfa_required": True}


async def test_a_token_without_an_aal_claim_is_refused(http, lawyer):
    r = await http.get("/users/", headers=bearer(http, lawyer, None))

    assert r.status_code == 403


async def test_a_session_that_passed_mfa_gets_in(http, lawyer):
    r = await http.get("/users/", headers=bearer(http, lawyer, "aal2"))

    assert r.status_code == 200


async def test_lawyer_routes_refuse_a_password_only_session(http, lawyer):
    r = await http.get("/admin/users", headers=bearer(http, lawyer, "aal1"))

    assert r.status_code == 403


class FakeAuth:
    async def list_mfa_factors(self, access_token, user):
        return MfaFactorsResponse(factors=[])

    async def verify_mfa(self, access_token, user, factor_id, code):
        if code != GOOD_CODE:
            raise UnauthorizedError("Invalid code. Please try again.")
        return SignInResponse(
            access_token="aal2-jwt",
            refresh_token="r",
            expires_in=3600,
            user=SignInUser(id=user.user_id, email=user.email, role=user.role),
            mfa_required=False,
        )

    async def remove_mfa_factor(self, access_token, user, factor_id):
        return MessageResponse(message="Authenticator removed.")

    async def sign_out(self, access_token, user):
        return MessageResponse(message="Signed out.")


@pytest.fixture
def fake_auth():
    app.dependency_overrides[get_auth_service] = lambda: FakeAuth()


async def test_the_mfa_endpoints_accept_a_password_only_session(
    http, fake_auth, lawyer
):
    headers = bearer(http, lawyer, "aal1")

    listed = await http.get("/auth/mfa/factors", headers=headers)
    verified = await http.post(
        "/auth/mfa/verify",
        json={"factor_id": "phone-app", "code": GOOD_CODE},
        headers=headers,
    )
    signed_out = await http.post("/auth/signout", headers=headers)

    assert (listed.status_code, verified.status_code, signed_out.status_code) == (
        200,
        200,
        200,
    )
    assert verified.json()["access_token"] == "aal2-jwt"


async def test_a_wrong_code_answers_401(http, fake_auth, lawyer):
    r = await http.post(
        "/auth/mfa/verify",
        json={"factor_id": "phone-app", "code": "000000"},
        headers=bearer(http, lawyer, "aal1"),
    )

    assert r.status_code == 401


async def test_removing_a_factor_needs_an_aal2_session(http, fake_auth, lawyer):
    aal1 = await http.delete(
        "/auth/mfa/factors/phone-app", headers=bearer(http, lawyer, "aal1")
    )
    aal2 = await http.delete(
        "/auth/mfa/factors/phone-app", headers=bearer(http, lawyer, "aal2")
    )

    assert (aal1.status_code, aal2.status_code) == (403, 200)


async def test_verify_is_throttled(http, fake_auth, lawyer):
    headers = bearer(http, lawyer, "aal1")
    codes = [
        (
            await http.post(
                "/auth/mfa/verify",
                json={"factor_id": "phone-app", "code": "000000"},
                headers=headers,
            )
        ).status_code
        for _ in range(6)
    ]

    assert codes == [401] * 5 + [429]
