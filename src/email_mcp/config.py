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
    smtp_cert_path: str = ""

    # Identity
    from_name: str = ""
    from_address: str = ""

    # Maildir
    maildir_root: Path = Path("~/.local/share/email-mcp/mail")

    @property
    def maildir_path(self) -> Path:
        return self.maildir_root.expanduser()

    # IMAP cert (shared between IMAP mutator and STARTTLS)
    imap_cert_path: str = ""

    # Sync
    sync_interval_seconds: int = 60
    sync_on_startup: bool = True
    full_sync_on_startup: bool = False
    mbsync_bin: str = "mbsync"
    mbsync_channel: str = "protonmail"

    # INBOX sync
    inbox_sync_interval: int = 60

    # Nightly sync
    nightly_sync_hour: int = 3
    nightly_sync_enabled: bool = True

    # IMAP IDLE
    idle_enabled: bool = True

    # Reindex debounce
    reindex_debounce: int = 60

    # Search
    notmuch_bin: str = "notmuch"

    # NTFY push notifications (empty URL = disabled)
    ntfy_url: str = ""
    ntfy_topic: str = ""

    # Server
    transport: Literal["stdio", "http"] = "stdio"
    host: str = "0.0.0.0"
    port: int = 10143
    log_level: str = "INFO"

    # Auth (optional, for HTTP transport)
    github_client_id: str | None = None
    github_client_secret: str | None = None
    oauth_base_url: str | None = None
    oauth_allowed_users: str | None = None
    oauth_state_dir: Path | None = None
