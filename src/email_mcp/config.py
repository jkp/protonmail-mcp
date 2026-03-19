"""Configuration via environment variables using pydantic-settings."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EMAIL_MCP_", env_file=".env", extra="ignore")

    # ProtonMail account email address
    imap_username: str = ""

    # Identity
    from_name: str = ""
    from_address: str = ""

    # SQLite database
    db_path: Path = Path("~/.local/share/email-mcp/email.db")

    @property
    def database_path(self) -> Path:
        return self.db_path.expanduser().resolve()

    # ProtonMail API (password only needed for initial auth, then cached in session)
    proton_password: str = ""
    proton_session_path: Path = Path("~/.local/share/email-mcp/proton_session.json")

    @property
    def proton_session_file(self) -> Path:
        return self.proton_session_path.expanduser().resolve()

    # ProtonMail web UI account index (for generating web URLs)
    proton_account_index: int = 0

    # Set to True to re-sync all message metadata on next startup
    # (backfills conversation_id, folder changes, etc. without re-indexing bodies)
    resync_metadata: bool = False

    # Set to True to re-embed all messages on next startup
    # (needed after changing what's included in embeddings)
    reembed: bool = False

    # Re-index content: bodies, headers, or both. One API call per message.
    # bodies=True re-decrypts unindexed bodies; headers=True backfills
    # ParsedHeaders for bulk email detection. Both can run together.
    reindex_bodies: bool = False
    reindex_headers: bool = False

    # NTFY push notifications (empty URL = disabled)
    ntfy_url: str = ""
    ntfy_topic: str = ""

    # Server
    transport: Literal["stdio", "http"] = "stdio"
    host: str = "0.0.0.0"
    port: int = 10143
    log_level: str = "INFO"
    reload: bool = False

    # Embedding API (optional — local model used as fallback)
    together_api_key: str = ""

    # Auth (optional, for HTTP transport)
    github_client_id: str | None = None
    github_client_secret: str | None = None
    oauth_base_url: str | None = None
    oauth_allowed_users: str | None = None
    oauth_state_dir: Path | None = None
