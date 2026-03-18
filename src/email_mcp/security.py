"""Security hardening middleware for MCP servers.

Strips server banners, blocks reconnaissance endpoints, adds
security headers, and rate-limits unauthenticated requests.

Reusable across any FastMCP/Starlette server exposed to the internet.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Endpoints that leak server metadata to unauthenticated users
_BLOCKED_PATHS = {
    "/register",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
}


class SecurityMiddleware(BaseHTTPMiddleware):
    """Hardens an ASGI app for public internet exposure.

    - Strips `server` header (hides uvicorn/python)
    - Blocks dynamic OAuth client registration
    - Blocks OAuth metadata discovery endpoints
    - Adds security headers (no sniff, no frame, etc.)
    - Rate-limits unauthenticated requests by IP
    - Returns minimal error responses (no implementation details)
    """

    def __init__(
        self,
        app: Any,
        rate_limit_rpm: int = 60,
        blocked_paths: set[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._rate_limit_rpm = rate_limit_rpm
        self._blocked_paths = blocked_paths or _BLOCKED_PATHS
        self._request_counts: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path

        # Block reconnaissance endpoints
        for blocked in self._blocked_paths:
            if path.startswith(blocked):
                return Response(status_code=404)

        # Rate limit unauthenticated requests by IP
        if "authorization" not in {k.lower() for k in request.headers.keys()}:
            client_ip = request.client.host if request.client else "unknown"
            if self._is_rate_limited(client_ip):
                return Response(status_code=429)

        response = await call_next(request)

        # Strip server identification
        if "server" in response.headers:
            del response.headers["server"]
        if "x-powered-by" in response.headers:
            del response.headers["x-powered-by"]

        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"

        # Minimise auth error details
        if response.status_code == 401:
            www_auth = response.headers.get("www-authenticate", "")
            if "error_description" in www_auth:
                response.headers["www-authenticate"] = 'Bearer error="invalid_token"'

        return response

    def _is_rate_limited(self, client_ip: str) -> bool:
        """Simple sliding window rate limiter."""
        now = time.monotonic()
        window = 60.0  # 1 minute
        timestamps = self._request_counts[client_ip]

        # Purge old entries
        self._request_counts[client_ip] = [
            t for t in timestamps if now - t < window
        ]
        timestamps = self._request_counts[client_ip]

        if len(timestamps) >= self._rate_limit_rpm:
            return True

        timestamps.append(now)
        return False
