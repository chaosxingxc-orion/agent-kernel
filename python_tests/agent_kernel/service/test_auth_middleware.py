"""Tests for ApiKeyMiddleware authentication layer."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agent_kernel.service.auth_middleware import ApiKeyMiddleware

# ---------------------------------------------------------------------------
# Minimal app fixture
# ---------------------------------------------------------------------------

_SECRET = "test-secret-key-42"


def _echo(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def _build_app(*, api_key: str | None) -> ApiKeyMiddleware:
    """Return a tiny Starlette app wrapped with ApiKeyMiddleware."""
    inner = Starlette(
        routes=[
            Route("/runs", _echo, methods=["GET", "POST"]),
            Route("/health/liveness", _echo, methods=["GET"]),
            Route("/health/readiness", _echo, methods=["GET"]),
            Route("/manifest", _echo, methods=["GET"]),
        ]
    )
    return ApiKeyMiddleware(inner, api_key=api_key)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestApiKeyMiddleware:
    def test_no_api_key_configured_allows_all(self) -> None:
        """When api_key is None, every request passes through."""
        app = _build_app(api_key=None)
        client = TestClient(app)
        resp = client.get("/runs")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_valid_api_key_passes(self) -> None:
        app = _build_app(api_key=_SECRET)
        client = TestClient(app)
        resp = client.get(
            "/runs",
            headers={"Authorization": f"Bearer {_SECRET}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_invalid_api_key_returns_401(self) -> None:
        app = _build_app(api_key=_SECRET)
        client = TestClient(app)
        resp = client.get(
            "/runs",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}

    def test_missing_auth_header_returns_401(self) -> None:
        app = _build_app(api_key=_SECRET)
        client = TestClient(app)
        resp = client.post("/runs")
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}

    def test_health_endpoints_exempt_from_auth(self) -> None:
        app = _build_app(api_key=_SECRET)
        client = TestClient(app)
        # No Authorization header — should still pass.
        for path in ("/health/liveness", "/health/readiness"):
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} should be exempt"
            assert resp.json() == {"ok": True}

    def test_manifest_exempt_from_auth(self) -> None:
        app = _build_app(api_key=_SECRET)
        client = TestClient(app)
        resp = client.get("/manifest")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
