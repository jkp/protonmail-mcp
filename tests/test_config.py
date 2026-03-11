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
