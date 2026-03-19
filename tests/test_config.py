"""Tests for email_mcp.config."""

from pathlib import Path

from email_mcp.config import Settings


def test_default_settings():
    """Settings loads without error and has expected field types."""
    settings = Settings()
    assert isinstance(settings.imap_username, str)
    assert settings.transport in ("stdio", "http")
    assert isinstance(settings.log_level, str)
    assert isinstance(settings.proton_password, str)


def test_database_path_expands_user():
    settings = Settings(db_path=Path("~/test.db"))
    assert "~" not in str(settings.database_path)
    assert str(settings.database_path).endswith("test.db")


def test_proton_session_path_expands_user():
    settings = Settings(proton_session_path=Path("~/session.json"))
    assert "~" not in str(settings.proton_session_file)
    assert str(settings.proton_session_file).endswith("session.json")


def test_env_prefix(monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_IMAP_USERNAME", "user@proton.me")
    monkeypatch.setenv("EMAIL_MCP_TRANSPORT", "http")
    settings = Settings()
    assert settings.imap_username == "user@proton.me"
    assert settings.transport == "http"


def test_proton_config_env_overrides(monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_PROTON_PASSWORD", "secret")
    monkeypatch.setenv("EMAIL_MCP_TOGETHER_API_KEY", "tok_123")
    settings = Settings()
    assert settings.proton_password == "secret"
    assert settings.together_api_key == "tok_123"


def test_old_env_vars_ignored(monkeypatch):
    """Removed env vars are silently ignored (extra='ignore')."""
    monkeypatch.setenv("EMAIL_MCP_MBSYNC_CHANNEL", "mymail")
    monkeypatch.setenv("EMAIL_MCP_SMTP_HOST", "localhost")
    monkeypatch.setenv("EMAIL_MCP_IMAP_HOST", "localhost")
    # Should not raise
    settings = Settings()
    assert settings.imap_username == ""
