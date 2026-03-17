"""FastMCP server instance and entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastmcp import FastMCP

from email_mcp.config import Settings
from email_mcp.logging import configure_logging
from email_mcp.store import MaildirStore

settings = Settings()
configure_logging(settings.log_level, ntfy_url=settings.ntfy_url, ntfy_topic=settings.ntfy_topic)

logger = structlog.get_logger()


def _ensure_notmuch_config(config_path: Path) -> None:
    """Generate notmuch config from server settings if it doesn't exist."""
    config_path.parent.mkdir(parents=True, exist_ok=True)

    config_text = f"""\
[database]
path={settings.maildir_path}

[new]
tags=unread;inbox
ignore=.mbsyncstate;.uidvalidity

[search]
exclude_tags=deleted;spam

[maildir]
synchronize_flags=true
"""
    config_path.write_text(config_text)
    logger.info("notmuch.config_generated", path=str(config_path))


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[None]:
    """Initialize IMAP mutator, sync engine, IDLE listener on startup."""
    import email_mcp.tools.batch as batch
    import email_mcp.tools.managing as managing
    from email_mcp.idle import IdleListener
    from email_mcp.imap import ImapMutator
    from email_mcp.sync import SyncEngine
    from email_mcp.tools.searching import _searcher

    notmuch_config = settings.maildir_path / ".notmuch" / "config"
    if not notmuch_config.exists():
        _ensure_notmuch_config(notmuch_config)
    notmuch_config_str = str(notmuch_config)

    # 1. Create IMAP mutator (fall back to SMTP cert for Bridge)
    imap_cert = settings.imap_cert_path or settings.smtp_cert_path
    imap = ImapMutator(
        host=settings.imap_host,
        port=settings.imap_port,
        username=settings.imap_username,
        password=settings.imap_password,
        starttls=settings.imap_starttls,
        cert_path=imap_cert,
    )

    # 2. Create sync engine
    sync_engine = SyncEngine(
        mbsync_bin=settings.mbsync_bin,
        notmuch_bin=settings.notmuch_bin,
        notmuch_config=notmuch_config_str,
        mbsync_channel=settings.mbsync_channel,
        reindex_debounce=settings.reindex_debounce,
    )

    # 3. Set module-level refs for tools
    managing._imap = imap
    managing._sync_engine = sync_engine
    managing._store = store
    managing._searcher = _searcher

    batch._imap = imap
    batch._sync_engine = sync_engine
    batch._store = store

    idle_listener: IdleListener | None = None

    try:
        # 4. Connect IMAP
        try:
            await imap.connect()
            logger.info("server.imap_connected")
        except Exception:
            logger.warning("server.imap_connect_failed", exc_info=True)

        # 5. Startup sync
        if settings.full_sync_on_startup:
            logger.info("server.full_sync_on_startup")
            try:
                await sync_engine.full_sync_and_rebuild(
                    maildir_root=str(settings.maildir_path)
                )
                logger.info("server.full_sync_on_startup.done")
            except Exception:
                logger.warning("server.full_sync_on_startup.failed", exc_info=True)
        elif settings.sync_on_startup:
            logger.info("server.startup_sync")
            try:
                await sync_engine.sync_inbox()
                logger.info("server.startup_sync.done")
            except Exception:
                logger.warning("server.startup_sync.failed", exc_info=True)
            # Background reindex
            sync_engine.request_reindex()

        # 7. Start IDLE listener if enabled
        if settings.idle_enabled:
            idle_listener = IdleListener(
                host=settings.imap_host,
                port=settings.imap_port,
                username=settings.imap_username,
                password=settings.imap_password,
                starttls=settings.imap_starttls,
                cert_path=imap_cert,
                on_change=sync_engine.sync_inbox,
            )
            try:
                await idle_listener.start()
                logger.info("server.idle_started")
            except Exception:
                logger.warning("server.idle_start_failed", exc_info=True)
                idle_listener = None

        # 8. Start periodic INBOX sync
        sync_engine.start_inbox_loop(interval=settings.inbox_sync_interval)

        # 9. Schedule nightly full sync
        if settings.nightly_sync_enabled:
            sync_engine.schedule_nightly(hour=settings.nightly_sync_hour)

        logger.info("server.ready")

        yield

    finally:
        # Shutdown
        if idle_listener:
            await idle_listener.stop()
        await sync_engine.stop()
        await imap.disconnect()
        managing._imap = None
        managing._sync_engine = None
        managing._store = None
        managing._searcher = None
        batch._imap = None
        batch._sync_engine = None
        batch._store = None
        logger.info("server.shutdown")


def _build_auth_storage(s: Settings):
    """Build persistent OAuth state storage if oauth_state_dir is configured.

    Returns a FileTreeStore pointed at the configured directory, or None
    to let FastMCP use its default (ephemeral across container restarts).
    """
    if s.oauth_state_dir is None:
        return None

    from key_value.aio.stores.filetree import (
        FileTreeStore,
        FileTreeV1CollectionSanitizationStrategy,
        FileTreeV1KeySanitizationStrategy,
    )

    state_dir = s.oauth_state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    logger.info("server.oauth_storage", path=str(state_dir))

    return FileTreeStore(
        data_directory=state_dir,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(state_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(
            state_dir
        ),
    )


def _build_auth():
    """Build OAuth auth provider if GitHub credentials are configured."""
    if not settings.github_client_id or not settings.github_client_secret:
        return None

    from fastmcp.server.auth.providers.github import GitHubProvider

    return GitHubProvider(
        client_id=settings.github_client_id,
        client_secret=settings.github_client_secret,
        base_url=settings.oauth_base_url or f"http://localhost:{settings.port}",
        client_storage=_build_auth_storage(settings),
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
            logger.warning("auth.rejected", reason="no_token")
            return False
        login = ctx.token.claims.get("login", "")
        if login in allowed:
            logger.info("auth.allowed", login=login)
            return True
        logger.warning("auth.rejected", login=login, reason="not_in_allowlist")
        return False

    return [AuthMiddleware(auth=require_allowed_user)]


mcp = FastMCP(
    name="Email MCP",
    auth=_build_auth(),
    middleware=_build_middleware(),
    lifespan=_lifespan,
)

store = MaildirStore(settings.maildir_path)

# Import tools to register them with the mcp instance
import email_mcp.tools.batch  # noqa: F401, E402
import email_mcp.tools.composing  # noqa: F401, E402
import email_mcp.tools.listing  # noqa: F401, E402
import email_mcp.tools.managing  # noqa: F401, E402
import email_mcp.tools.reading  # noqa: F401, E402
import email_mcp.tools.searching  # noqa: F401, E402


def main() -> None:
    """Entry point for the MCP server."""
    logger.info(
        "server.starting",
        transport=settings.transport,
        host=settings.host,
        port=settings.port,
        auth_enabled=settings.github_client_id is not None,
        log_level=settings.log_level,
        maildir_root=str(settings.maildir_path),
    )
    if settings.transport == "http":
        mcp.run(transport="http", host=settings.host, port=settings.port)
    else:
        mcp.run()
