"""Configuration via environment variables using pydantic-settings."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EMAIL_MCP_", env_file=".env", extra="ignore"
    )

    # IMAP (for mbsync)
    imap_host: str = "127.0.0.1"
    imap_port: int = 1143
    imap_username: str = ""
    imap_password: str = ""
    imap_starttls: bool = True

    # SMTP (for sending)
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False

    # Identity
    from_name: str = ""
    from_address: str = ""

    # Maildir
    maildir_root: Path = Path("~/.local/share/email-mcp/mail")

    @property
    def maildir_path(self) -> Path:
        return self.maildir_root.expanduser()

    # Sync
    sync_interval_seconds: int = 60
    sync_on_startup: bool = True
    mbsync_bin: str = "mbsync"

    # Search
    notmuch_bin: str = "notmuch"

    # Server
    transport: Literal["stdio", "http"] = "stdio"
    host: str = "0.0.0.0"
    port: int = 8025
    log_level: str = "INFO"

    # Auth (optional, for HTTP transport)
    github_client_id: str | None = None
    github_client_secret: str | None = None
    oauth_base_url: str | None = None
    oauth_allowed_users: str | None = None
