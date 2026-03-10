"""FastMCP server instance and entry point."""

from fastmcp import FastMCP

from protonmail_mcp.config import Settings
from protonmail_mcp.himalaya import HimalayaClient
from protonmail_mcp.notmuch import NotmuchSearcher

settings = Settings()

mcp = FastMCP(name="ProtonMail MCP")

himalaya = HimalayaClient(
    bin_path=settings.himalaya_bin,
    timeout=settings.himalaya_timeout,
    account=settings.himalaya_account,
    config_path=settings.himalaya_config_path,
)

notmuch = NotmuchSearcher(
    bin_path=settings.notmuch_bin,
    maildir_root=settings.maildir_root or "",
    timeout=settings.notmuch_timeout,
)

# Import tools to register them with the mcp instance
import protonmail_mcp.tools.composing  # noqa: F401, E402
import protonmail_mcp.tools.listing  # noqa: F401, E402
import protonmail_mcp.tools.managing  # noqa: F401, E402
import protonmail_mcp.tools.reading  # noqa: F401, E402
import protonmail_mcp.tools.searching  # noqa: F401, E402


def main() -> None:
    """Entry point for the MCP server."""
    if settings.transport == "http":
        mcp.run(transport="http", host=settings.host, port=settings.port)
    else:
        mcp.run()
