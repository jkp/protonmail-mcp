"""Security hardening middleware for MCP servers.

Strips server banners, blocks client registration after first setup,
adds security headers, and rate-limits unauthenticated requests.

Reusable across any FastMCP/Starlette server exposed to the internet.
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class SecurityMiddleware(BaseHTTPMiddleware):
    """Hardens an ASGI app for public internet exposure.

    - Strips `server` header (hides uvicorn/python)
    - Blocks /register once a client is already registered
      (allows first-time OAuth setup, locks the door after)
    - Adds security headers (no sniff, no frame, etc.)
    - Rate-limits unauthenticated requests by IP
    - Minimises auth error details
    """

    def __init__(
        self,
        app: Any,
        rate_limit_rpm: int = 60,
        oauth_state_dir: Path | None = None,
    ) -> None:
        super().__init__(app)
        self._rate_limit_rpm = rate_limit_rpm
        self._oauth_state_dir = oauth_state_dir
        self._request_counts: dict[str, list[float]] = defaultdict(list)

    def _has_registered_clients(self) -> bool:
        """Check if any OAuth clients have been registered."""
        if not self._oauth_state_dir or not self._oauth_state_dir.exists():
            return False
        return any(self._oauth_state_dir.iterdir())

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path

        # Block /register if a client is already registered.
        # First-time setup needs it, but after that lock the door.
        if path == "/register" and self._has_registered_clients():
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
