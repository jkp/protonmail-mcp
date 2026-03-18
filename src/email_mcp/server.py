"""FastMCP server instance and entry point.

v4 startup sequence:
1. Open SQLite database
2. Create ProtonMail API client (load saved session)
3. Connect Bridge IMAP (for body indexer only)
4. Run InitialSync (idempotent — no-op if already done)
5. Start EventLoop background task (ProtonMail event polling)
6. Start BodyIndexer worker queue (IMAP body fetch + FTS index)
7. Accept MCP connections
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastmcp import FastMCP

from email_mcp.config import Settings
from email_mcp.db import Database
from email_mcp.logging import configure_logging
from email_mcp.store import MaildirStore

settings = Settings()
_log_file = settings.database_path.parent / "email-mcp.log"
configure_logging(settings.log_level, ntfy_url=settings.ntfy_url, ntfy_topic=settings.ntfy_topic, log_file=_log_file)

logger = structlog.get_logger()


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[None]:
    """Initialize v4 components: ProtonMail API, event loop, body indexer."""
    import email_mcp.tools.batch as batch
    import email_mcp.tools.managing as managing
    import email_mcp.tools.reading as reading
    from email_mcp.body_indexer import BodyIndexer
    from email_mcp.event_loop import EventLoop
    from email_mcp.imap import ImapMutator
    from email_mcp.initial_sync import InitialSync
    from email_mcp.progress import SyncProgress
    from email_mcp.proton_api import AuthError, ProtonClient

    # 1. Create ProtonMail API client (loads session from disk)
    api = ProtonClient(
        username=settings.imap_username,
        password=settings.proton_password,
        session_path=settings.proton_session_file,
    )

    # 2. Connect Bridge IMAP for body indexer
    imap_cert = settings.imap_cert_path or settings.smtp_cert_path
    imap = ImapMutator(
        host=settings.imap_host,
        port=settings.imap_port,
        username=settings.imap_username,
        password=settings.imap_password,
        starttls=settings.imap_starttls,
        cert_path=imap_cert,
    )

    # 3. Set refs on tools (imap now defined)
    managing._api = api
    batch._api = api
    reading._imap = imap

    progress = SyncProgress()
    event_loop = EventLoop(db=db, api=api)
    body_indexer = BodyIndexer(db=db, imap=imap, workers=3, progress=progress)
    initial_sync = InitialSync(db=db, api=api, body_indexer=body_indexer, progress=progress)

    background_tasks: list[asyncio.Task] = []

    try:
        # 4. Connect Bridge IMAP (non-fatal if unavailable)
        try:
            await imap.connect()
            logger.info("server.imap_connected")
        except Exception:
            logger.warning("server.imap_connect_failed", exc_info=True)

        # 5. Validate API session (non-fatal — may need re-auth)
        try:
            await api.get_latest_event_id()
            logger.info("server.api_session_valid")
        except AuthError:
            logger.warning("server.api_auth_required",
                           detail="Run 'email-mcp auth' to authenticate with ProtonMail")
        except Exception:
            logger.warning("server.api_check_failed", exc_info=True)

        # 6a. Always sync labels (fast, resolves custom folder names)
        try:
            await initial_sync.sync_labels()
        except Exception:
            logger.warning("server.label_sync_failed", exc_info=True)

        # 6b. Initial sync (idempotent — no-op if already completed)
        try:
            with progress:
                await initial_sync.run()
        except Exception:
            logger.warning("server.initial_sync_failed", exc_info=True)

        async def _bulk_reindex_bodies() -> None:
            """Background task: bulk re-index folders with unindexed bodies, then fall back to queue."""
            unindexed_folders = db.execute(
                "SELECT DISTINCT folder FROM messages"
                " WHERE body_indexed = 0 AND message_id IS NOT NULL"
                "   AND folder IS NOT NULL AND folder != 'All Mail'"
            ).fetchall()
            if not unindexed_folders:
                return
            folders = [r[0] for r in unindexed_folders]
            logger.info("server.bulk_reindex_start", folders=folders)
            try:
                with progress:
                    progress.set_bodies_total(
                        db.execute(
                            "SELECT COUNT(*) FROM messages WHERE body_indexed = 0 AND message_id IS NOT NULL"
                        ).fetchone()[0]
                    )
                    for folder in folders:
                        await body_indexer.index_folder(folder)
            except Exception:
                logger.warning("server.bulk_reindex_failed", exc_info=True)
            # Queue any still-unindexed messages for per-message retry
            still_unindexed = db.execute(
                "SELECT pm_id FROM messages WHERE body_indexed = 0 AND message_id IS NOT NULL"
            ).fetchall()
            queued = 0
            for (pm_id,) in still_unindexed:
                try:
                    event_loop.body_queue.put_nowait(pm_id)
                    queued += 1
                except asyncio.QueueFull:
                    break
            logger.info("server.bulk_reindex_done", queued_remaining=queued)

        async def _requeue_unindexed_loop() -> None:
            while True:
                await asyncio.sleep(6 * 3600)
                rows = db.execute(
                    "SELECT pm_id FROM messages WHERE body_indexed = 0 AND message_id IS NOT NULL"
                ).fetchall()
                queued = 0
                for (pm_id,) in rows:
                    try:
                        event_loop.body_queue.put_nowait(pm_id)
                        queued += 1
                    except asyncio.QueueFull:
                        break
                if queued:
                    logger.info("server.requeued_unindexed_periodic", count=queued)

        # 7b. Start bulk body re-index as background task (non-blocking)
        background_tasks.append(
            asyncio.create_task(_bulk_reindex_bodies(), name="bulk_reindex_bodies")
        )

        # 8. Start event loop background task
        background_tasks.append(
            asyncio.create_task(event_loop.run(), name="event_loop")
        )

        # 9. Start body indexer worker queue
        background_tasks.append(
            asyncio.create_task(
                body_indexer.run_workers(event_loop.body_queue), name="body_indexer"
            )
        )

        # 10. Periodic requeue of un-indexed bodies
        background_tasks.append(
            asyncio.create_task(_requeue_unindexed_loop(), name="requeue_unindexed")
        )

        logger.info("server.ready")
        yield

    finally:
        # Cancel background tasks
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)

        # Send sentinel to drain body queue workers cleanly
        try:
            for _ in range(body_indexer._workers):
                event_loop.body_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

        await imap.disconnect()
        managing._api = None
        batch._api = None
        reading._imap = None
        logger.info("server.shutdown")


def _build_auth_storage(s: Settings):
    """Build persistent OAuth state storage if oauth_state_dir is configured."""
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


def _cached_verify_token(provider, ttl: int = 300):
    """Wrap a provider's verify_token with a TTL cache."""
    import time

    cache: dict[str, tuple[float, object]] = {}
    original = provider.verify_token.__func__  # unbound method

    async def cached(self, token: str):
        now = time.monotonic()
        if token in cache:
            cached_at, result = cache[token]
            if now - cached_at < ttl:
                return result
        result = await original(self, token)
        if result is not None:
            cache[token] = (now, result)
        return result

    import types

    provider.verify_token = types.MethodType(cached, provider)


def _build_auth():
    """Build OAuth auth provider if GitHub credentials are configured."""
    if not settings.github_client_id or not settings.github_client_secret:
        return None

    from fastmcp.server.auth.providers.github import GitHubProvider

    provider = GitHubProvider(
        client_id=settings.github_client_id,
        client_secret=settings.github_client_secret,
        base_url=settings.oauth_base_url or f"http://localhost:{settings.port}",
        client_storage=_build_auth_storage(settings),
    )
    _cached_verify_token(provider, ttl=300)
    return provider


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
db = Database(settings.database_path)

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
        db_path=str(settings.database_path),
    )
    if settings.transport == "http":
        mcp.run(transport="http", host=settings.host, port=settings.port, stateless_http=True)
    else:
        mcp.run()
