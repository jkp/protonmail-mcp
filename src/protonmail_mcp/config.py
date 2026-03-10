"""Configuration via environment variables using pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="", env_nested_delimiter="__", env_file=".env", extra="ignore"
    )

    # Himalaya
    himalaya_bin: str = "himalaya"
    himalaya_config_path: str | None = None
    himalaya_account: str | None = None
    himalaya_timeout: int = 30

    # Notmuch
    notmuch_bin: str = "notmuch"
    notmuch_config: str | None = None
    maildir_root: str | None = None
    notmuch_timeout: int = 30

    # OAuth (GitHub)
    github_client_id: str | None = None
    github_client_secret: str | None = None
    oauth_base_url: str | None = None
    oauth_allowed_users: str | None = None  # comma-separated GitHub usernames

    # Logging
    log_level: str = "INFO"

    # MCP transport
    protonmail_mcp_transport: str = "stdio"
    protonmail_mcp_host: str = "0.0.0.0"
    protonmail_mcp_port: int = 10143
