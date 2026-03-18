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
    import json

    import email_mcp.tools.batch as batch
    import email_mcp.tools.managing as managing
    import email_mcp.tools.reading as reading
    from email_mcp.body_indexer import BodyIndexer
    from email_mcp.crypto import ProtonKeyRing, derive_mailbox_passphrase
    from email_mcp.decryptor import ProtonDecryptor
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

    # 2. Connect Bridge IMAP (still needed for mutations + attachment download)
    imap_cert = settings.imap_cert_path or settings.smtp_cert_path
    imap = ImapMutator(
        host=settings.imap_host,
        port=settings.imap_port,
        username=settings.imap_username,
        password=settings.imap_password,
        starttls=settings.imap_starttls,
        cert_path=imap_cert,
    )

    # 3. Set refs on tools
    managing._api = api
    batch._api = api
    reading._imap = imap

    progress = SyncProgress()
    event_loop = EventLoop(db=db, api=api)

    background_tasks: list[asyncio.Task] = []

    try:
        # 4. Connect Bridge IMAP (non-fatal — only needed for mutations)
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

        # 5b. Load PGP keys for message decryption (non-fatal)
        decryptor: ProtonDecryptor | None = None
        try:
            session_data = json.loads(settings.proton_session_file.read_text())
            key_salts = session_data.get("key_salts", {})
            user_data = await api.get_user()
            user_key = user_data["Keys"][0]
            key_salt = key_salts.get(user_key["ID"])
            if key_salt:
                passphrase = derive_mailbox_passphrase(settings.proton_password, key_salt)
            else:
                passphrase = settings.proton_password
            key_ring = ProtonKeyRing(user_key["PrivateKey"], passphrase)

            # Load address keys
            addresses = await api.get_addresses()
            for addr in addresses:
                for ak in addr.get("Keys", []):
                    token = ak.get("Token", "")
                    if token:
                        try:
                            key_ring.add_address_key(ak["PrivateKey"], token)
                        except Exception:
                            logger.debug("server.address_key_skip", email=addr.get("Email"), exc_info=True)

            decryptor = ProtonDecryptor(api=api, key_ring=key_ring)
            logger.info("server.keys_loaded", address_keys=len(addresses))
        except Exception:
            logger.warning("server.key_load_failed", exc_info=True,
                           detail="Body decryption unavailable — run 'email-mcp-auth' with 2FA")

        body_indexer = BodyIndexer(db=db, decryptor=decryptor, workers=3, progress=progress) if decryptor else None
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

        async def _bulk_reindex_bodies() -> None:
            """Background task: bulk-fetch and decrypt all unindexed bodies via API."""
            if not body_indexer:
                logger.warning("server.bulk_reindex_skipped", reason="no decryptor")
                return
            unindexed_count = db.execute(
                "SELECT COUNT(*) FROM messages WHERE body_indexed = 0"
            ).fetchone()[0]
            if not unindexed_count:
                return
            logger.info("server.bulk_reindex_start", count=unindexed_count)
            try:
                with progress:
                    progress.set_bodies_total(unindexed_count)
                    await body_indexer.index_unindexed()
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

        # 8. Start event loop background task
        background_tasks.append(
            asyncio.create_task(event_loop.run(), name="event_loop")
        )

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
