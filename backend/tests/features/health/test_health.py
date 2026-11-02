import importlib

import httpx
import pytest

from app.main import app

# The package re-exports `router` (the APIRouter), which shadows the module.
health = importlib.import_module("app.features.health.router")


async def _get(path: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as http:
        return await http.get(path)


async def _ok() -> None:
    pass


async def _down() -> None:
    raise ConnectionError("refused")


async def test_liveness_needs_nothing():
    r = await _get("/health")
    assert (r.status_code, r.json()) == (200, {"status": "ok"})


async def test_ready_when_every_dependency_answers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(health, "CHECKS", {"database": _ok, "redis": _ok, "workers": _ok})
    r = await _get("/health/ready")
    assert r.status_code == 200
    assert r.json()["checks"] == {"database": "ok", "redis": "ok", "workers": "ok"}


async def test_not_ready_names_what_is_down(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        health, "CHECKS", {"database": _ok, "redis": _ok, "workers": _down}
    )
    r = await _get("/health/ready")
    assert r.status_code == 503
    assert r.json() == {
        "status": "degraded",
        "checks": {"database": "ok", "redis": "ok", "workers": "down"},
    }
