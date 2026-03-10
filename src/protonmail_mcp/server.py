"""FastMCP server instance and entry point."""

from fastmcp import FastMCP

from protonmail_mcp.config import Settings
from protonmail_mcp.himalaya import HimalayaClient
from protonmail_mcp.notmuch import NotmuchSearcher

settings = Settings()


def _build_auth():
    """Build OAuth auth provider if GitHub credentials are configured."""
    if not settings.github_client_id or not settings.github_client_secret:
        return None

    from fastmcp.server.auth.providers.github import GitHubProvider

    return GitHubProvider(
        client_id=settings.github_client_id,
        client_secret=settings.github_client_secret,
        base_url=settings.oauth_base_url or f"http://localhost:{settings.port}",
    )


def _build_middleware():
    """Build auth middleware with user allowlist if configured."""
    if not settings.oauth_allowed_users:
        return []

    allowed = {u.strip() for u in settings.oauth_allowed_users.split(",")}

    from fastmcp.server.auth import AuthContext
    from fastmcp.server.middleware import AuthMiddleware

    def require_allowed_user(ctx: AuthContext) -> bool:
        if ctx.token is None:
            return False
        login = ctx.token.claims.get("login", "")
        return login in allowed

    return [AuthMiddleware(auth=require_allowed_user)]


mcp = FastMCP(
    name="ProtonMail MCP",
    auth=_build_auth(),
    middleware=_build_middleware(),
)

himalaya = HimalayaClient(
    bin_path=settings.himalaya_bin,
    timeout=settings.himalaya_timeout,
    account=settings.himalaya_account,
    config_path=settings.himalaya_config_path,
)

notmuch = NotmuchSearcher(
    bin_path=settings.notmuch_bin,
    config_path=settings.notmuch_config,
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
