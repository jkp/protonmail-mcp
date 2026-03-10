"""Tests for configuration module."""

import os
from unittest.mock import patch

from protonmail_mcp.config import Settings


class TestSettings:
    def test_defaults(self) -> None:
        """Settings should have sensible defaults."""
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings()
        assert settings.himalaya_bin == "himalaya"
        assert settings.himalaya_timeout == 30
        assert settings.notmuch_bin == "notmuch"
        assert settings.notmuch_timeout == 30
        assert settings.transport == "stdio"
        assert settings.host == "0.0.0.0"
        assert settings.port == 8000

    def test_env_override(self) -> None:
        """Settings should be overridable via environment variables."""
        env = {
            "HIMALAYA_BIN": "/usr/local/bin/himalaya",
            "HIMALAYA_CONFIG_PATH": "/custom/config.toml",
            "HIMALAYA_ACCOUNT": "work",
            "HIMALAYA_TIMEOUT": "60",
            "NOTMUCH_BIN": "/usr/local/bin/notmuch",
            "MAILDIR_ROOT": "/home/user/maildir",
            "NOTMUCH_TIMEOUT": "45",
            "PROTONMAIL_MCP_TRANSPORT": "http",
            "PROTONMAIL_MCP_HOST": "127.0.0.1",
            "PROTONMAIL_MCP_PORT": "9000",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings()
        assert settings.himalaya_bin == "/usr/local/bin/himalaya"
        assert settings.himalaya_config_path == "/custom/config.toml"
        assert settings.himalaya_account == "work"
        assert settings.himalaya_timeout == 60
        assert settings.notmuch_bin == "/usr/local/bin/notmuch"
        assert settings.maildir_root == "/home/user/maildir"
        assert settings.notmuch_timeout == 45
        assert settings.transport == "http"
        assert settings.host == "127.0.0.1"
        assert settings.port == 9000

    def test_optional_fields_default_none(self) -> None:
        """Optional fields should default to None."""
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings()
        assert settings.himalaya_config_path is None
        assert settings.himalaya_account is None
        assert settings.maildir_root is None
