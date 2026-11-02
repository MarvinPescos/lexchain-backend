import httpx
from starlette.requests import Request

from app.features.auth.dependencies import get_auth_service
from app.features.auth.schemas import MessageResponse
from app.infrastructure.cache import client_ip, limiter
from app.main import app


def _request(headers: dict[str, str], peer: str = "172.18.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": (peer, 1234),
        }
    )


def test_key_is_the_address_the_proxy_appended():
    forged = _request({"X-Forwarded-For": "1.1.1.1, 203.0.113.7"})
    assert client_ip(forged) == "203.0.113.7"


def test_key_falls_back_to_the_peer_without_a_proxy():
    assert client_ip(_request({})) == "172.18.0.1"


class FakeAuth:
    async def resend_verification_email(self, email):
        return MessageResponse(message="sent")


async def test_resend_verification_is_throttled_per_client():
    limiter.reset()
    app.dependency_overrides[get_auth_service] = lambda: FakeAuth()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as http:
            codes = [
                (
                    await http.post(
                        "/auth/resend-verification",
                        json={"email": "someone@example.com"},
                        headers={"X-Forwarded-For": "203.0.113.7"},
                    )
                ).status_code
                for _ in range(4)
            ]
            other = await http.post(
                "/auth/resend-verification",
                json={"email": "someone@example.com"},
                headers={"X-Forwarded-For": "203.0.113.8"},
            )
    finally:
        app.dependency_overrides.clear()
        limiter.reset()

    assert codes == [200, 200, 200, 429]
    assert other.status_code == 200
