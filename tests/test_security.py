"""Tests for security hardening middleware."""

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from email_mcp.security import SecurityMiddleware


def _make_app(
    rate_limit_rpm: int = 60,
    oauth_state_dir: Path | None = None,
) -> Starlette:
    """Build a minimal Starlette app with SecurityMiddleware."""

    async def homepage(request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def auth_fail(request: Request) -> Response:
        resp = JSONResponse({"error": "unauthorized"}, status_code=401)
        resp.headers["www-authenticate"] = (
            'Bearer error="invalid_token", '
            'error_description="Token expired at 2026-01-01T00:00:00Z"'
        )
        return resp

    app = Starlette(
        routes=[
            Route("/mcp", homepage, methods=["GET", "POST"]),
            Route("/register", homepage, methods=["GET", "POST"]),
            Route("/.well-known/{path:path}", homepage),
            Route("/auth-fail", auth_fail),
        ],
    )
    app.add_middleware(
        SecurityMiddleware,
        rate_limit_rpm=rate_limit_rpm,
        oauth_state_dir=oauth_state_dir,
    )
    return app


@pytest.fixture
def oauth_dir(tmp_path: Path) -> Path:
    """OAuth state dir with a registered client."""
    state_dir = tmp_path / "oauth"
    state_dir.mkdir()
    (state_dir / "client_abc.json").write_text("{}")
    return state_dir


@pytest.fixture
def client(oauth_dir: Path) -> TestClient:
    return TestClient(_make_app(oauth_state_dir=oauth_dir))


class TestRegistration:
    """/register is open — oauth_allowed_users is the real protection."""

    def test_register_allowed(self, client: TestClient) -> None:
        resp = client.post("/register", json={"redirect_uris": ["https://example.com"]})
        assert resp.status_code == 200


class TestWellKnownEndpoints:
    """/.well-known/ endpoints pass through (needed for OAuth discovery)."""

    def test_well_known_oauth_server_allowed(self, client: TestClient) -> None:
        resp = client.get("/.well-known/oauth-authorization-server")
        assert resp.status_code == 200

    def test_well_known_resource_allowed(self, client: TestClient) -> None:
        resp = client.get("/.well-known/oauth-protected-resource")
        assert resp.status_code == 200


class TestServerHeaderStripping:
    def test_no_server_header(self, client: TestClient) -> None:
        resp = client.post(
            "/mcp",
            json={},
            headers={"Authorization": "Bearer test"},
        )
        assert "server" not in resp.headers

    def test_no_x_powered_by_header(self, client: TestClient) -> None:
        resp = client.post(
            "/mcp",
            json={},
            headers={"Authorization": "Bearer test"},
        )
        assert "x-powered-by" not in resp.headers


class TestSecurityHeaders:
    def test_x_content_type_options(self, client: TestClient) -> None:
        resp = client.post("/mcp", json={}, headers={"Authorization": "Bearer test"})
        assert resp.headers.get("x-content-type-options") == "nosniff"

    def test_x_frame_options(self, client: TestClient) -> None:
        resp = client.post("/mcp", json={}, headers={"Authorization": "Bearer test"})
        assert resp.headers.get("x-frame-options") == "DENY"

    def test_referrer_policy(self, client: TestClient) -> None:
        resp = client.post("/mcp", json={}, headers={"Authorization": "Bearer test"})
        assert resp.headers.get("referrer-policy") == "no-referrer"

    def test_cache_control(self, client: TestClient) -> None:
        resp = client.post("/mcp", json={}, headers={"Authorization": "Bearer test"})
        assert resp.headers.get("cache-control") == "no-store"


class TestAuthErrorMinimisation:
    def test_www_authenticate_stripped(self, client: TestClient) -> None:
        resp = client.get("/auth-fail", headers={"Authorization": "Bearer test"})
        assert resp.status_code == 401
        www_auth = resp.headers.get("www-authenticate", "")
        assert "error_description" not in www_auth
        assert 'Bearer error="invalid_token"' in www_auth


class TestRateLimiting:
    def test_unauthenticated_rate_limited(self, oauth_dir: Path) -> None:
        app = _make_app(rate_limit_rpm=3, oauth_state_dir=oauth_dir)
        c = TestClient(app)
        for _ in range(3):
            resp = c.post("/mcp", json={})
            assert resp.status_code == 200
        resp = c.post("/mcp", json={})
        assert resp.status_code == 429

    def test_authenticated_not_rate_limited(self, oauth_dir: Path) -> None:
        app = _make_app(rate_limit_rpm=3, oauth_state_dir=oauth_dir)
        c = TestClient(app)
        for _ in range(5):
            resp = c.post(
                "/mcp",
                json={},
                headers={"Authorization": "Bearer test"},
            )
            assert resp.status_code == 200


class TestNormalTraffic:
    def test_mcp_endpoint_works(self, client: TestClient) -> None:
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer test"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
