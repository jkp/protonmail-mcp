"""Tests for OAuth auth configuration."""

import os
from unittest.mock import patch

from protonmail_mcp.server import _build_auth, _build_middleware


class TestBuildAuth:
    def test_returns_none_without_credentials(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            from protonmail_mcp import server

            server.settings = server.Settings(_env_file=None)
            assert _build_auth() is None

    def test_returns_none_with_partial_credentials(self) -> None:
        with patch.dict(os.environ, {"GITHUB_CLIENT_ID": "id_only"}, clear=True):
            from protonmail_mcp import server

            server.settings = server.Settings(_env_file=None)
            assert _build_auth() is None

    def test_returns_provider_with_full_credentials(self) -> None:
        env = {
            "GITHUB_CLIENT_ID": "test_id",
            "GITHUB_CLIENT_SECRET": "test_secret",
            "OAUTH_BASE_URL": "https://example.com",
        }
        with patch.dict(os.environ, env, clear=True):
            from protonmail_mcp import server

            server.settings = server.Settings(_env_file=None)
            provider = _build_auth()
            assert provider is not None


class TestBuildMiddleware:
    def test_returns_empty_without_allowlist(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            from protonmail_mcp import server

            server.settings = server.Settings(_env_file=None)
            assert _build_middleware() == []

    def test_returns_middleware_with_allowlist(self) -> None:
        with patch.dict(os.environ, {"OAUTH_ALLOWED_USERS": "alice,bob"}, clear=True):
            from protonmail_mcp import server

            server.settings = server.Settings(_env_file=None)
            middleware = _build_middleware()
            assert len(middleware) == 1

    def test_allowlist_check_allows_valid_user(self) -> None:
        with patch.dict(os.environ, {"OAUTH_ALLOWED_USERS": "alice,bob"}, clear=True):
            from protonmail_mcp import server

            server.settings = server.Settings(_env_file=None)
            middleware = _build_middleware()
            # Extract the auth check function from the middleware
            auth_fn = middleware[0].auth
            from unittest.mock import MagicMock

            ctx = MagicMock()
            ctx.token.claims = {"login": "alice"}
            assert auth_fn(ctx) is True

    def test_allowlist_check_rejects_unknown_user(self) -> None:
        with patch.dict(os.environ, {"OAUTH_ALLOWED_USERS": "alice,bob"}, clear=True):
            from protonmail_mcp import server

            server.settings = server.Settings(_env_file=None)
            middleware = _build_middleware()
            auth_fn = middleware[0].auth
            from unittest.mock import MagicMock

            ctx = MagicMock()
            ctx.token.claims = {"login": "mallory"}
            assert auth_fn(ctx) is False

    def test_allowlist_check_rejects_no_token(self) -> None:
        with patch.dict(os.environ, {"OAUTH_ALLOWED_USERS": "alice"}, clear=True):
            from protonmail_mcp import server

            server.settings = server.Settings(_env_file=None)
            middleware = _build_middleware()
            auth_fn = middleware[0].auth
            from unittest.mock import MagicMock

            ctx = MagicMock()
            ctx.token = None
            assert auth_fn(ctx) is False

    def test_allowlist_handles_whitespace(self) -> None:
        with patch.dict(os.environ, {"OAUTH_ALLOWED_USERS": " alice , bob "}, clear=True):
            from protonmail_mcp import server

            server.settings = server.Settings(_env_file=None)
            middleware = _build_middleware()
            auth_fn = middleware[0].auth
            from unittest.mock import MagicMock

            ctx = MagicMock()
            ctx.token.claims = {"login": "bob"}
            assert auth_fn(ctx) is True
