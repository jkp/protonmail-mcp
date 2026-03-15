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
    assert settings.sync_interval_seconds == 60
    assert settings.imap_cert_path == ""
    assert settings.inbox_sync_interval == 60
    assert settings.nightly_sync_hour == 3
    assert settings.nightly_sync_enabled is True
    assert settings.idle_enabled is True
    assert settings.reindex_debounce == 60
    assert settings.mbsync_channel == "protonmail"


def test_maildir_path_expands_user():
    settings = Settings(maildir_root=Path("~/test-mail"))
    assert "~" not in str(settings.maildir_path)
    assert str(settings.maildir_path).endswith("test-mail")


def test_env_prefix(monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_IMAP_HOST", "mail.example.com")
    monkeypatch.setenv("EMAIL_MCP_SMTP_PORT", "587")
    monkeypatch.setenv("EMAIL_MCP_TRANSPORT", "http")
    settings = Settings()
    assert settings.imap_host == "mail.example.com"
    assert settings.smtp_port == 587
    assert settings.transport == "http"


def test_v3_config_env_overrides(monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_IMAP_CERT_PATH", "/tmp/cert.pem")
    monkeypatch.setenv("EMAIL_MCP_INBOX_SYNC_INTERVAL", "30")
    monkeypatch.setenv("EMAIL_MCP_NIGHTLY_SYNC_HOUR", "4")
    monkeypatch.setenv("EMAIL_MCP_NIGHTLY_SYNC_ENABLED", "false")
    monkeypatch.setenv("EMAIL_MCP_IDLE_ENABLED", "false")
    monkeypatch.setenv("EMAIL_MCP_REINDEX_DEBOUNCE", "120")
    monkeypatch.setenv("EMAIL_MCP_MBSYNC_CHANNEL", "mymail")
    settings = Settings()
    assert settings.imap_cert_path == "/tmp/cert.pem"
    assert settings.inbox_sync_interval == 30
    assert settings.nightly_sync_hour == 4
    assert settings.nightly_sync_enabled is False
    assert settings.idle_enabled is False
    assert settings.reindex_debounce == 120
    assert settings.mbsync_channel == "mymail"
