"""Sync engine: tiered mbsync + debounced notmuch reindex."""

import asyncio
import time
from datetime import UTC, datetime, timedelta

import structlog

from email_mcp.models import SyncStatus

logger = structlog.get_logger()


class SyncError(Exception):
    """Error during sync operations."""


class SyncEngine:
    """Tiered sync: per-folder mbsync, singleton pattern, debounced notmuch."""

    def __init__(
        self,
        mbsync_bin: str = "mbsync",
        notmuch_bin: str = "notmuch",
        mbsync_config: str | None = None,
        notmuch_config: str | None = None,
        mbsync_channel: str = "protonmail",
        timeout: int = 300,
        reindex_debounce: int | float = 60,
    ) -> None:
        self.mbsync_bin = mbsync_bin
        self.notmuch_bin = notmuch_bin
        self.mbsync_config = mbsync_config
        self.notmuch_config = notmuch_config
        self.mbsync_channel = mbsync_channel
        self.timeout = timeout
        self.reindex_debounce = reindex_debounce
        self._lock = asyncio.Lock()
        self._status = SyncStatus()
        self._running = False
        self._dirty = False
        self._inbox_task: asyncio.Task[None] | None = None
        self._nightly_task: asyncio.Task[None] | None = None
        self._reindex_handle: asyncio.TimerHandle | None = None
        self._reindex_running = False

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
                "sync.error",
                returncode=proc.returncode,
                stderr=err,
                elapsed_s=round(elapsed, 2),
            )
            raise SyncError(f"{cmd[0]} failed: {err}")

        log.info("sync.ok", elapsed_s=round(elapsed, 2))
        return stdout.decode()

    def _mbsync_cmd(self, folder: str | None = None) -> list[str]:
        """Build the mbsync command for a folder or all folders."""
        if folder:
            target = f"{self.mbsync_channel}:{folder}"
        else:
            target = self.mbsync_channel
        cmd = [self.mbsync_bin]
        if self.mbsync_config:
            cmd.extend(["-c", self.mbsync_config])
        cmd.append(target)
        return cmd

    async def _do_sync(self, folder: str | None = None) -> None:
        """Run mbsync (optionally per-folder) + notmuch new + count."""
        self._status.state = "syncing"
        self._status.error = None

        try:
            mbsync_cmd = self._mbsync_cmd(folder)
            try:
                await self._run_command(*mbsync_cmd)
            except SyncError as e:
                logger.warning("sync.mbsync_partial", error=str(e))
            self._status.last_sync = datetime.now(UTC)

            await self._run_command(self.notmuch_bin, "new")
            self._status.last_index = datetime.now(UTC)

            result = await self._run_command(self.notmuch_bin, "count", "*")
            self._status.message_count = int(result.strip())

            self._status.state = "ready"
        except SyncError as e:
            self._status.state = "error"
            self._status.error = str(e)
            raise

    async def sync(self) -> None:
        """Run a full sync cycle: mbsync -a + notmuch new (backward compat)."""
        async with self._lock:
            # Use -a for backward compat with the old full sync
            self._status.state = "syncing"
            self._status.error = None
            try:
                mbsync_cmd = [self.mbsync_bin, "-a"]
                if self.mbsync_config:
                    mbsync_cmd = [self.mbsync_bin, "-c", self.mbsync_config, "-a"]
                try:
                    await self._run_command(*mbsync_cmd)
                except SyncError as e:
                    logger.warning("sync.mbsync_partial", error=str(e))
                self._status.last_sync = datetime.now(UTC)

                await self._run_command(self.notmuch_bin, "new")
                self._status.last_index = datetime.now(UTC)

                result = await self._run_command(self.notmuch_bin, "count", "*")
                self._status.message_count = int(result.strip())

                self._status.state = "ready"
            except SyncError as e:
                self._status.state = "error"
                self._status.error = str(e)
                raise

    async def sync_inbox(self) -> None:
        """Sync only INBOX: mbsync channel:INBOX + notmuch new."""
        async with self._lock:
            await self._do_sync(folder="INBOX")

    async def sync_all(self) -> None:
        """Sync all folders: mbsync channel + notmuch new."""
        async with self._lock:
            await self._do_sync(folder=None)

    async def full_sync_and_rebuild(self, maildir_root: str | None = None) -> None:
        """Full mbsync -a + notmuch database rebuild.

        Reconciles local Maildir with IMAP server state, then deletes
        the notmuch database and rebuilds from scratch so folder tags
        match actual file locations.
        """
        async with self._lock:
            self._status.state = "syncing"
            self._status.error = None
            try:
                # 1. Full mbsync (all channels, all folders)
                mbsync_cmd = [self.mbsync_bin]
                if self.mbsync_config:
                    mbsync_cmd.extend(["-c", self.mbsync_config])
                mbsync_cmd.append("-a")
                try:
                    await self._run_command(*mbsync_cmd)
                except SyncError as e:
                    logger.warning("sync.mbsync_partial", error=str(e))
                self._status.last_sync = datetime.now(UTC)

                # 2. Delete notmuch database to force full rebuild
                if maildir_root:
                    import shutil
                    from pathlib import Path

                    db_path = Path(maildir_root) / ".notmuch" / "xapian"
                    if db_path.exists():
                        logger.info("sync.removing_index", path=str(db_path))
                        shutil.rmtree(db_path)

                # 3. Rebuild: notmuch new scans everything fresh (needs longer timeout)
                logger.info("sync.rebuilding_index")
                old_timeout = self.timeout
                self.timeout = max(self.timeout, 600)
                try:
                    await self._run_command(self.notmuch_bin, "new")
                finally:
                    self.timeout = old_timeout
                self._status.last_index = datetime.now(UTC)

                result = await self._run_command(self.notmuch_bin, "count", "*")
                self._status.message_count = int(result.strip())

                self._status.state = "ready"
                logger.info("sync.full_rebuild_done", message_count=self._status.message_count)
            except SyncError as e:
                self._status.state = "error"
                self._status.error = str(e)
                raise

    async def request_sync(self, folder: str | None = None) -> None:
        """Request a sync with singleton coalescing.

        If a sync is already running, sets a dirty flag.
        After the running sync completes, it re-runs if dirty.
        """
        if self._running:
            self._dirty = True
            return
        self._running = True
        try:
            async with self._lock:
                await self._do_sync(folder)
                while self._dirty:
                    self._dirty = False
                    await self._do_sync(folder)
        finally:
            self._running = False

    def request_reindex(self) -> None:
        """Request a debounced notmuch reindex."""
        # Cancel any pending timer
        if self._reindex_handle is not None:
            self._reindex_handle.cancel()

        loop = asyncio.get_event_loop()
        self._reindex_handle = loop.call_later(
            self.reindex_debounce, self._fire_reindex
        )

    def _fire_reindex(self) -> None:
        """Timer callback: schedule the actual reindex coroutine."""
        self._reindex_handle = None
        asyncio.create_task(self._do_reindex())

    async def _do_reindex(self) -> None:
        """Run notmuch new (at most 1 concurrent)."""
        if self._reindex_running:
            return
        self._reindex_running = True
        try:
            await self._run_command(self.notmuch_bin, "new")
            self._status.last_index = datetime.now(UTC)
        except SyncError:
            logger.warning("sync.reindex_failed", exc_info=True)
        finally:
            self._reindex_running = False

    def start_inbox_loop(self, interval: int | float = 60) -> None:
        """Start a periodic INBOX sync loop."""

        async def _loop() -> None:
            while True:
                try:
                    await self.sync_inbox()
                except SyncError:
                    pass  # Already logged
                await asyncio.sleep(interval)

        self._inbox_task = asyncio.create_task(_loop())

    def schedule_nightly(self, hour: int = 3) -> None:
        """Schedule a nightly full sync at the given hour."""

        async def _nightly() -> None:
            while True:
                now = datetime.now(UTC)
                # Calculate seconds until next target hour
                target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
                if target <= now:
                    target = target + timedelta(days=1)
                wait_seconds = (target - now).total_seconds()
                logger.info(
                    "sync.nightly_scheduled",
                    next_run=target.isoformat(),
                    wait_s=wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
                try:
                    await self.sync_all()
                except SyncError:
                    pass  # Already logged

        self._nightly_task = asyncio.create_task(_nightly())

    async def stop(self) -> None:
        """Stop all background tasks."""
        for attr in ("_inbox_task", "_nightly_task"):
            task = getattr(self, attr, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)

        if self._reindex_handle is not None:
            self._reindex_handle.cancel()
            self._reindex_handle = None

    # Keep backward compat aliases
    async def start_sync_loop(self, interval: int = 60) -> None:
        """Start a periodic sync loop (backward compat)."""
        self.start_inbox_loop(interval)

    async def stop_sync_loop(self) -> None:
        """Stop the periodic sync loop (backward compat)."""
        await self.stop()
