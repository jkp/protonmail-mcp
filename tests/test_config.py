"""Tests for email_mcp.config."""

from pathlib import Path

from email_mcp.config import Settings


def test_default_settings():
    settings = Settings()
    assert settings.imap_host == "127.0.0.1"
    assert settings.imap_port == 1143
    assert settings.smtp_host == "127.0.0.1"
    assert settings.smtp_port == 1025
    assert settings.transport == "stdio"
    assert settings.log_level == "INFO"
    assert settings.imap_cert_path == ""
    assert settings.proton_password == ""


def test_maildir_path_expands_user():
    settings = Settings(maildir_root=Path("~/test-mail"))
    assert "~" not in str(settings.maildir_path)
    assert str(settings.maildir_path).endswith("test-mail")


def test_database_path_expands_user():
    settings = Settings(db_path=Path("~/test.db"))
    assert "~" not in str(settings.database_path)
    assert str(settings.database_path).endswith("test.db")


def test_proton_session_path_expands_user():
    settings = Settings(proton_session_path=Path("~/session.json"))
    assert "~" not in str(settings.proton_session_file)
    assert str(settings.proton_session_file).endswith("session.json")


def test_env_prefix(monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_IMAP_HOST", "mail.example.com")
    monkeypatch.setenv("EMAIL_MCP_SMTP_PORT", "587")
    monkeypatch.setenv("EMAIL_MCP_TRANSPORT", "http")
    settings = Settings()
    assert settings.imap_host == "mail.example.com"
    assert settings.smtp_port == 587
    assert settings.transport == "http"


def test_v4_config_env_overrides(monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_IMAP_CERT_PATH", "/tmp/cert.pem")
    monkeypatch.setenv("EMAIL_MCP_PROTON_PASSWORD", "secret")
    settings = Settings()
    assert settings.imap_cert_path == "/tmp/cert.pem"
    assert settings.proton_password == "secret"


def test_old_v3_env_vars_ignored(monkeypatch):
    """Removed v3 env vars are silently ignored (extra='ignore')."""
    monkeypatch.setenv("EMAIL_MCP_MBSYNC_CHANNEL", "mymail")
    monkeypatch.setenv("EMAIL_MCP_NIGHTLY_SYNC_ENABLED", "false")
    monkeypatch.setenv("EMAIL_MCP_IDLE_ENABLED", "false")
    # Should not raise
    settings = Settings()
    assert settings.imap_host == "127.0.0.1"
