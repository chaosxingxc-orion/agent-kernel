"""Verifies for the openapi specification generator."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.service.http_server import create_app
from agent_kernel.service.openapi import generate_openapi_spec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Number of routes registered in create_app (excluding /openapi.json itself,
# the spec includes it as a documented path).
# From http_server.py: 27 Route() calls + 1 openapi.json = 28 routes total.
# However /runs/{run_id}/children has GET and POST on the same path, so the
# route list has 28 Route entries but only 27 unique path strings (children
# path appears twice).  The OpenAPI spec collapses them into one path key
# with two methods.
_EXPECTED_PATH_COUNT = 27


def _make_facade() -> KernelFacade:
    """Make facade."""
    gw = MagicMock()
    return KernelFacade(workflow_gateway=gw)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_openapi_spec_valid():
    """generate_openapi_spec returns a well-formed OpenAPI 3.1 dict."""
    spec = generate_openapi_spec()

    assert spec["openapi"] == "3.1.0"
    assert "info" in spec
    assert spec["info"]["title"] == "Agent Kernel API"
    assert "paths" in spec

    paths = spec["paths"]
    assert isinstance(paths, dict)
    assert len(paths) == _EXPECTED_PATH_COUNT

    # Every path entry must have at least one HTTP method.
    for path, methods in paths.items():
        assert isinstance(methods, dict), f"{path} has no methods"
        for method, op in methods.items():
            assert method in {"get", "post", "put", "patch", "delete"}, (
                f"unexpected method {method!r} on {path}"
            )
            assert "operationId" in op, f"{method.upper()} {path} missing operationId"
            assert "responses" in op, f"{method.upper()} {path} missing responses"


@pytest.mark.anyio()
async def test_openapi_endpoint():
    """GET /openapi.json returns 200 with a valid spec."""
    app = create_app(_make_facade())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        resp = await client.get("/openapi.json")

    assert resp.status_code == 200
    body = resp.json()
    assert body["openapi"] == "3.1.0"
    assert "paths" in body


@pytest.mark.anyio()
async def test_openapi_endpoint_exempt_from_auth():
    """GET /openapi.json bypasses API key auth."""
    app = create_app(_make_facade(), api_key="secret-key-123")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # No Authorization header — should still succeed.
        resp = await client.get("/openapi.json")

    assert resp.status_code == 200
    assert resp.json()["openapi"] == "3.1.0"
