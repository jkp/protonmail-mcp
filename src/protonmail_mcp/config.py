"""Configuration via environment variables using pydantic-settings."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_nested_delimiter="__")

    # Himalaya
    himalaya_bin: str = "himalaya"
    himalaya_config_path: str | None = None
    himalaya_account: str | None = None
    himalaya_timeout: int = 30

    # Notmuch
    notmuch_bin: str = "notmuch"
    maildir_root: str | None = None
    notmuch_timeout: int = 30

    # MCP transport (prefixed env vars)
    transport: str = Field(default="stdio", validation_alias="PROTONMAIL_MCP_TRANSPORT")
    host: str = Field(default="0.0.0.0", validation_alias="PROTONMAIL_MCP_HOST")
    port: int = Field(default=8000, validation_alias="PROTONMAIL_MCP_PORT")
