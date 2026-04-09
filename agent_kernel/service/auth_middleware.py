"""API key authentication middleware for the kernel HTTP service.

Validates Bearer tokens from the Authorization header. When no API key is
configured (``api_key is None``), all requests pass through unchanged —
authentication is opt-in.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

# Paths that never require authentication (health probes + read-only introspection).
_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/health/liveness",
        "/health/readiness",
        "/manifest",
        "/metrics",
    }
)


class ApiKeyMiddleware:
    """Starlette middleware that validates API key from Authorization header.

    When api_key is None, authentication is disabled (open access).
    Health/liveness endpoints are always exempt.
    """

    def __init__(self, app: ASGIApp, *, api_key: str | None) -> None:
        self._app = app
        self._api_key = api_key

    @property
    def state(self):
        """Proxy ``state`` to the wrapped application for test compatibility."""
        return self._app.state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._api_key is None:
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if path in _EXEMPT_PATHS:
            await self._app(scope, receive, send)
            return

        # Extract Authorization header from raw ASGI headers.
        headers = dict(scope.get("headers", []))
        auth_value = headers.get(b"authorization", b"").decode("latin-1")

        if auth_value == f"Bearer {self._api_key}":
            await self._app(scope, receive, send)
            return

        # Reject: send 401 JSON response.
        body = json.dumps({"error": "unauthorized"}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(body)).encode()],
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
            }
        )
