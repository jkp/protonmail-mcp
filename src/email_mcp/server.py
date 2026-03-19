"""FastMCP server instance and entry point.

v5 startup sequence:
1. Open SQLite database
2. Create ProtonMail API client (load saved session)
3. Load PGP keys for message decryption
4. Run InitialSync (idempotent — no-op if already done)
5. Start EventLoop background task (ProtonMail event polling)
6. Start BodyIndexer worker queue (API fetch + PGP decrypt + FTS index)
7. Accept MCP connections

No Bridge IMAP dependency — all operations use the ProtonMail REST API directly.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastmcp import FastMCP

from email_mcp.config import Settings
from email_mcp.db import Database
from email_mcp.logging import configure_logging

settings = Settings()
_log_file = settings.database_path.parent / "email-mcp.log"
configure_logging(
    settings.log_level,
    ntfy_url=settings.ntfy_url,
    ntfy_topic=settings.ntfy_topic,
    log_file=_log_file,
)

logger = structlog.get_logger()


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[None]:
    """Initialize v5 components: ProtonMail API, PGP keys, event loop, body indexer."""
    import json

    import email_mcp.tools.batch as batch
    import email_mcp.tools.composing as composing
    import email_mcp.tools.managing as managing
    import email_mcp.tools.reading as reading
    from email_mcp.body_indexer import BodyIndexer
    from email_mcp.crypto import ProtonKeyRing, derive_mailbox_passphrase
    from email_mcp.decryptor import ProtonDecryptor
    from email_mcp.event_loop import EventLoop
    from email_mcp.initial_sync import InitialSync
    from email_mcp.progress import SyncProgress
    from email_mcp.proton_api import AuthError, ProtonClient

    # 1. Create ProtonMail API client (loads session from disk)
    api = ProtonClient(
        username=settings.imap_username,
        password=settings.proton_password,
        session_path=settings.proton_session_file,
    )

    # 2. Set refs on tools
    managing._api = api
    batch._api = api
    managing._event_loop = None  # Set after EventLoop is created

    progress = SyncProgress(transport=settings.transport)
    event_loop = EventLoop(db=db, api=api)
    managing._event_loop = event_loop

    background_tasks: list[asyncio.Task] = []

    try:
        # 3. Validate API session (non-fatal — may need re-auth)
        try:
            await api.get_latest_event_id()
            logger.info("server.api_session_valid")
        except AuthError:
            logger.warning(
                "server.api_auth_required",
                detail="Run 'email-mcp auth' to authenticate with ProtonMail",
            )
        except Exception:
            logger.warning("server.api_check_failed", exc_info=True)

        # 4. Load PGP keys for message decryption (non-fatal)
        decryptor: ProtonDecryptor | None = None
        try:
            session_data = json.loads(settings.proton_session_file.read_text())
            passphrase = session_data.get("mailbox_passphrase", "")
            if not passphrase:
                # Fallback: derive from password + key salt (legacy sessions)
                key_salts = session_data.get("key_salts", {})
                user_data = await api.get_user()
                user_key_id = user_data["Keys"][0]["ID"]
                key_salt = key_salts.get(user_key_id)
                if key_salt and settings.proton_password:
                    passphrase = derive_mailbox_passphrase(settings.proton_password, key_salt)
                elif settings.proton_password:
                    passphrase = settings.proton_password
                else:
                    raise ValueError(
                        "No mailbox_passphrase in session and no password configured. "
                        "Re-run email-mcp-auth."
                    )
            user_data = await api.get_user()
            user_key = user_data["Keys"][0]
            key_ring = ProtonKeyRing(user_key["PrivateKey"], passphrase)

            # Load address keys
            addresses = await api.get_addresses()
            for addr in addresses:
                for ak in addr.get("Keys", []):
                    token = ak.get("Token", "")
                    if token:
                        try:
                            key_ring.add_address_key(
                                ak["PrivateKey"], token, email=addr.get("Email", "")
                            )
                        except Exception:
                            logger.debug(
                                "server.address_key_skip",
                                email=addr.get("Email"),
                                exc_info=True,
                            )

            decryptor = ProtonDecryptor(api=api, key_ring=key_ring)
            reading._decryptor = decryptor

            # Wire up ProtonSender for composing tools
            from email_mcp.sender import ProtonSender

            composing._sender = ProtonSender(api=api, key_ring=key_ring)
            logger.info("server.keys_loaded", address_keys=len(addresses))
        except Exception:
            logger.warning(
                "server.key_load_failed",
                exc_info=True,
                detail="Body decryption unavailable — run 'email-mcp-auth' with 2FA",
            )

        body_indexer = (
            BodyIndexer(db=db, decryptor=decryptor, workers=3, progress=progress)
            if decryptor
            else None
        )
        initial_sync = InitialSync(db=db, api=api, body_indexer=body_indexer, progress=progress)

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

        # 6c. Metadata re-sync (backfills conversation_id, etc.)
        if settings.resync_metadata:
            from email_mcp.event_loop import _event_to_row
            from email_mcp.proton_api import derive_folder

            async def _resync_metadata() -> None:
                """Re-sync all message metadata from ProtonMail API."""
                logger.info("server.resync_metadata.start")
                label_map = {
                    lbl["ID"]: {"name": lbl["Name"], "type": lbl.get("Type", 1)}
                    for lbl in await api.get_labels()
                }
                page = 0
                synced = 0
                while True:
                    messages, total = await api.get_messages(page=page, page_size=150)
                    if not messages:
                        break
                    for msg in messages:
                        pm_id = msg["ID"]
                        label_ids = msg.get("LabelIDs", [])
                        folder = derive_folder(label_ids, label_map)
                        row = _event_to_row(pm_id, msg, folder)
                        db.messages.upsert(row)
                    synced += len(messages)
                    if synced % 1500 == 0 or synced >= total:
                        logger.info("server.resync_metadata.progress", synced=synced, total=total)
                    if synced >= total:
                        break
                    page += 1
                logger.info("server.resync_metadata.done", synced=synced)

            background_tasks.append(asyncio.create_task(_resync_metadata(), name="resync_metadata"))

        async def _bulk_reindex_bodies() -> None:
            """Background task: index unindexed bodies in priority order.

            INBOX first, then Sent/Archive/labels, then folder=NULL last.
            """
            if not body_indexer:
                logger.warning("server.bulk_reindex_skipped", reason="no decryptor")
                return
            unindexed_count = db.execute(
                "SELECT COUNT(*) FROM messages WHERE body_indexed = 0"
            ).fetchone()[0]
            if not unindexed_count:
                return

            # Priority order: INBOX first, then real folders, then NULL
            priority_folders = db.execute(
                "SELECT folder, COUNT(*) as cnt FROM messages"
                " WHERE body_indexed = 0"
                " GROUP BY folder"
                " ORDER BY"
                "   CASE"
                "     WHEN folder = 'INBOX' THEN 0"
                "     WHEN folder = 'Sent' THEN 1"
                "     WHEN folder = 'Drafts' THEN 2"
                "     WHEN folder IS NOT NULL THEN 3"
                "     ELSE 4"
                "   END"
            ).fetchall()

            logger.info(
                "server.bulk_reindex_start",
                count=unindexed_count,
                folders=[f"{r[0] or 'NULL'}:{r[1]}" for r in priority_folders],
            )
            try:
                with progress:
                    progress.set_bodies_total(unindexed_count)
                    for folder_row in priority_folders:
                        folder = folder_row[0]
                        await body_indexer.index_unindexed(folder=folder)
            except Exception:
                logger.warning("server.bulk_reindex_failed", exc_info=True)
            remaining = db.execute(
                "SELECT COUNT(*) FROM messages WHERE body_indexed = 0"
            ).fetchone()[0]
            logger.info("server.bulk_reindex_done", remaining=remaining)

        # 7b. Start bulk body re-index as background task (non-blocking)
        background_tasks.append(
            asyncio.create_task(_bulk_reindex_bodies(), name="bulk_reindex_bodies")
        )

        # 7c. Start embedding pipeline (downstream of body indexer)
        embedder = None
        try:
            from email_mcp.embedder import Embedder

            embedder = Embedder(db=db, api_key=settings.together_api_key)
            import email_mcp.tools.searching as searching_mod

            searching_mod._embedder = embedder
            logger.info("server.embedder_loaded")
        except Exception:
            logger.warning("server.embedder_load_failed", exc_info=True)

        # 7d. Re-index content (bodies and/or headers in one pass)
        if (settings.reindex_bodies or settings.reindex_headers) and body_indexer:

            async def _reindex_content() -> None:
                logger.info(
                    "server.reindex_content.start",
                    bodies=settings.reindex_bodies,
                    headers=settings.reindex_headers,
                )
                await body_indexer.reindex_content(
                    bodies=settings.reindex_bodies,
                    headers=settings.reindex_headers,
                )
                logger.info("server.reindex_content.done")

            background_tasks.append(asyncio.create_task(_reindex_content(), name="reindex_content"))

        # 7e. Reset embeddings if reembed flag is set
        if settings.reembed and embedder:
            reset_count = db.execute(
                "UPDATE messages SET embedded = 0 WHERE embedded != 0"
            ).rowcount
            db.commit()
            logger.info("server.reembed_reset", count=reset_count)

        # 7e. Warm up search models in background (non-blocking)
        if embedder:

            async def _warmup_models() -> None:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, embedder.warmup)

            background_tasks.append(asyncio.create_task(_warmup_models(), name="warmup_models"))

        # Threshold: if more than this many messages are unembedded, use
        # Together API for speed. Below this, use local model (free).
        backfill_threshold = 10

        async def _embed_bodies() -> None:
            """Background task: embed body-indexed messages for vector search.

            Uses Together API while working through a large backlog, then
            switches to local model for ongoing trickle (no API costs).
            """
            if not embedder:
                return
            import concurrent.futures
            from functools import partial

            pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="embedder"
            )
            loop = asyncio.get_event_loop()
            try:
                while True:
                    pm_ids = embedder.get_unembedded(limit=100)
                    if not pm_ids:
                        await asyncio.sleep(30)
                        continue
                    use_api = len(pm_ids) >= backfill_threshold
                    logger.info(
                        "server.embed_starting",
                        batch=len(pm_ids),
                        mode="api" if use_api else "local",
                    )
                    count = await loop.run_in_executor(
                        pool, partial(embedder.embed_batch, pm_ids, use_api=use_api)
                    )
                    logger.info(
                        "server.embed_progress",
                        embedded=count,
                        batch=len(pm_ids),
                        mode="api" if use_api else "local",
                    )
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pool.shutdown(wait=False, cancel_futures=True)
                raise

        background_tasks.append(asyncio.create_task(_embed_bodies(), name="embed_bodies"))

        # 8. Start event loop background task
        background_tasks.append(asyncio.create_task(event_loop.run(), name="event_loop"))

        # 9. Start body indexer worker queue (for ongoing events)
        if body_indexer:
            background_tasks.append(
                asyncio.create_task(
                    body_indexer.run_workers(event_loop.body_queue), name="body_indexer"
                )
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
        if body_indexer:
            try:
                for _ in range(body_indexer._workers):
                    event_loop.body_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

        managing._api = None
        managing._event_loop = None
        batch._api = None
        reading._decryptor = None
        composing._sender = None
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
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(state_dir),
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

db = Database(settings.database_path)

# Import tools to register them with the mcp instance
import email_mcp.tools.batch  # noqa: F401, E402
import email_mcp.tools.composing  # noqa: F401, E402
import email_mcp.tools.listing  # noqa: F401, E402
import email_mcp.tools.managing  # noqa: F401, E402
import email_mcp.tools.reading  # noqa: F401, E402
import email_mcp.tools.searching  # noqa: F401, E402


def _build_app():
    """Build the ASGI app with middleware (used by uvicorn reload mode)."""
    from email_mcp.security import SecurityMiddleware

    _app = mcp.http_app(transport="http", stateless_http=True)
    _app.add_middleware(SecurityMiddleware, oauth_state_dir=settings.oauth_state_dir)  # type: ignore[arg-type]
    return _app


# Module-level app for uvicorn --reload (needs importable path)
app = _build_app() if settings.transport == "http" else None


def main() -> None:
    """Entry point for the MCP server."""
    import faulthandler
    import signal

    faulthandler.enable()
    # SIGUSR1 dumps all thread stacks to stderr
    faulthandler.register(signal.SIGUSR1)

    logger.info(
        "server.starting",
        transport=settings.transport,
        host=settings.host,
        port=settings.port,
        auth_enabled=settings.github_client_id is not None,
        log_level=settings.log_level,
        db_path=str(settings.database_path),
    )
    try:
        if settings.transport == "http":
            import uvicorn

            if settings.reload:
                # Reload mode: pass import string so uvicorn can re-import on changes
                uvicorn.run(
                    "email_mcp.server:app",
                    host=settings.host,
                    port=settings.port,
                    timeout_graceful_shutdown=0,
                    log_level="info",
                    reload=True,
                    reload_dirs=["src"],
                )
            else:
                assert app is not None, "app must be built for http transport"
                uvicorn.run(
                    app,
                    host=settings.host,
                    port=settings.port,
                    timeout_graceful_shutdown=0,
                    log_level="info",
                )
        else:
            mcp.run(log_level="WARNING")
    except (KeyboardInterrupt, SystemExit):
        logger.info("server.shutdown")
    except Exception:
        logger.error("server.crashed", exc_info=True)
        raise
