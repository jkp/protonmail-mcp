"""Sync manager: mbsync + notmuch new subprocess management."""

import asyncio
import time
from datetime import UTC, datetime

import structlog

from email_mcp.models import SyncStatus

logger = structlog.get_logger()


class SyncError(Exception):
    """Error during sync operations."""


class SyncManager:
    """Manages mbsync and notmuch new as subprocesses."""

    def __init__(
        self,
        mbsync_bin: str = "mbsync",
        notmuch_bin: str = "notmuch",
        mbsync_config: str | None = None,
        notmuch_config: str | None = None,
        timeout: int = 300,
    ) -> None:
        self.mbsync_bin = mbsync_bin
        self.notmuch_bin = notmuch_bin
        self.mbsync_config = mbsync_config
        self.notmuch_config = notmuch_config
        self.timeout = timeout
        self._lock = asyncio.Lock()
        self._status = SyncStatus()
        self._sync_task: asyncio.Task[None] | None = None

    @property
    def status(self) -> SyncStatus:
        return self._status

    async def _run_command(self, *cmd: str) -> str:
        """Run a subprocess command and return stdout."""
        log = logger.bind(cmd=cmd[0])
        log.debug("sync.exec", cmd=list(cmd))

        env = None
        if self.notmuch_config and cmd[0] == self.notmuch_bin:
            import os

            env = {**os.environ, "NOTMUCH_CONFIG": self.notmuch_config}

        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except TimeoutError:
            proc.kill()
            elapsed = time.monotonic() - t0
            log.error("sync.timeout", elapsed_s=round(elapsed, 2))
            raise SyncError(f"{cmd[0]} timed out after {self.timeout}s")

        elapsed = time.monotonic() - t0

        if proc.returncode != 0:
            err = stderr.decode().strip()
            log.error(
                "sync.error", returncode=proc.returncode,
                stderr=err, elapsed_s=round(elapsed, 2),
            )
            raise SyncError(f"{cmd[0]} failed: {err}")

        log.info("sync.ok", elapsed_s=round(elapsed, 2))
        return stdout.decode()

    async def sync(self) -> None:
        """Run a full sync cycle: mbsync + notmuch new."""
        async with self._lock:
            self._status.state = "syncing"
            self._status.error = None

            try:
                # Run mbsync
                mbsync_cmd = [self.mbsync_bin, "-a"]
                if self.mbsync_config:
                    mbsync_cmd = [self.mbsync_bin, "-c", self.mbsync_config, "-a"]
                await self._run_command(*mbsync_cmd)
                self._status.last_sync = datetime.now(UTC)

                # Run notmuch new
                await self._run_command(self.notmuch_bin, "new")
                self._status.last_index = datetime.now(UTC)

                # Get message count
                result = await self._run_command(self.notmuch_bin, "count", "*")
                self._status.message_count = int(result.strip())

                self._status.state = "ready"
            except SyncError as e:
                self._status.state = "error"
                self._status.error = str(e)
                raise

    async def start_sync_loop(self, interval: int = 60) -> None:
        """Start a periodic sync loop."""

        async def _loop() -> None:
            while True:
                try:
                    await self.sync()
                except SyncError:
                    pass  # Already logged and stored in status
                await asyncio.sleep(interval)

        self._sync_task = asyncio.create_task(_loop())

    async def stop_sync_loop(self) -> None:
        """Stop the periodic sync loop."""
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            self._sync_task = None
