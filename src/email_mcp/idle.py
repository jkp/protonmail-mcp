"""IMAP IDLE listener for real-time INBOX change notifications.

Uses IMAPClient (sync) in a dedicated thread since aioimaplib lacks STARTTLS.
"""

import asyncio
import ssl
from collections.abc import Callable, Coroutine
from typing import Any

import structlog
from imapclient import IMAPClient

logger = structlog.get_logger()

# Re-issue IDLE every 29 minutes per RFC 2177
_IDLE_TIMEOUT = 29 * 60


class IdleListener:
    """Listen for IMAP IDLE notifications on INBOX."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 1143,
        username: str = "",
        password: str = "",
        starttls: bool = True,
        cert_path: str = "",
        on_change: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.starttls = starttls
        self.cert_path = cert_path
        self.on_change = on_change
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._tls_context: ssl.SSLContext | None = None
        if cert_path:
            import os

            self._tls_context = ssl.create_default_context(
                cafile=os.path.expanduser(cert_path)
            )

    def _connect_sync(self) -> IMAPClient:
        """Create and authenticate an IMAP connection for IDLE (sync)."""
        client = IMAPClient(self.host, port=self.port, ssl=False)
        if self.starttls:
            ctx = self._tls_context or ssl.create_default_context()
            client.starttls(ssl_context=ctx)
        client.login(self.username, self.password)
        client.select_folder("INBOX")
        return client

    def _idle_wait_sync(self, client: IMAPClient) -> list[tuple[int, bytes]]:
        """Block in IDLE waiting for server push (sync, runs in thread)."""
        client.idle()
        responses = client.idle_check(timeout=_IDLE_TIMEOUT)
        client.idle_done()
        return responses

    async def _idle_loop(self) -> None:
        """Main IDLE loop: connect, IDLE, wait for push, call on_change."""
        backoff = 1
        while not self._stop_event.is_set():
            client = None
            try:
                client = await asyncio.to_thread(self._connect_sync)
                backoff = 1
                logger.info("idle.connected")

                while not self._stop_event.is_set():
                    responses = await asyncio.to_thread(
                        self._idle_wait_sync, client
                    )
                    if responses and self.on_change:
                        logger.info(
                            "idle.change_detected",
                            count=len(responses),
                        )
                        try:
                            await self.on_change()
                        except Exception:
                            logger.warning(
                                "idle.on_change_failed", exc_info=True
                            )

            except asyncio.CancelledError:
                if client:
                    try:
                        await asyncio.to_thread(client.logout)
                    except Exception:
                        pass
                return
            except Exception:
                logger.warning("idle.error", backoff=backoff, exc_info=True)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=backoff
                    )
                    return  # stop was requested
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 300)

    async def start(self) -> None:
        """Start the IDLE listener as a background task."""
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._idle_loop())
        logger.info("idle.started")

    async def stop(self) -> None:
        """Stop the IDLE listener."""
        if self._task is not None:
            self._stop_event.set()
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("idle.stopped")
