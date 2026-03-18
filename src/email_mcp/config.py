"""Configuration via environment variables using pydantic-settings."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EMAIL_MCP_", env_file=".env", extra="ignore"
    )

    # Bridge IMAP (for body fetching only — decrypts PGP transparently)
    imap_host: str = "127.0.0.1"
    imap_port: int = 1143
    imap_username: str = ""
    imap_password: str = ""
    imap_starttls: bool = True
    imap_cert_path: str = ""

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

    # Maildir (kept for composing reply/forward originals via Bridge)
    maildir_root: Path = Path("~/.local/share/email-mcp/mail")

    @property
    def maildir_path(self) -> Path:
        return self.maildir_root.expanduser().resolve()

    # v4 SQLite database
    db_path: Path = Path("~/.local/share/email-mcp/email.db")

    @property
    def database_path(self) -> Path:
        return self.db_path.expanduser().resolve()

    # v4 ProtonMail native API
    # imap_username is the ProtonMail email address (used for both Bridge IMAP and API)
    # proton_password: actual ProtonMail account password (for SRP auth)
    # imap_password: Bridge-generated app password (for IMAP body fetching only)
    proton_password: str = ""
    proton_session_path: Path = Path("~/.local/share/email-mcp/proton_session.json")

    @property
    def proton_session_file(self) -> Path:
        return self.proton_session_path.expanduser().resolve()

    # NTFY push notifications (empty URL = disabled)
    ntfy_url: str = ""
    ntfy_topic: str = ""

    # Server
    transport: Literal["stdio", "http"] = "stdio"
    host: str = "0.0.0.0"
    port: int = 10143
    log_level: str = "INFO"

    # Embedding API (optional — local model used as fallback)
    together_api_key: str = ""

    # Auth (optional, for HTTP transport)
    github_client_id: str | None = None
    github_client_secret: str | None = None
    oauth_base_url: str | None = None
    oauth_allowed_users: str | None = None
    oauth_state_dir: Path | None = None
