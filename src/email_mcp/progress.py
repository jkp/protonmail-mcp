"""Interactive progress display for sync and body indexing.

When stderr is a TTY, shows rich progress bars. Otherwise is a no-op so
structured JSON logs flow through normally (MCP stdio mode).
"""

from __future__ import annotations

import sys
from types import TracebackType
from typing import Any


def is_interactive() -> bool:
    return sys.stderr.isatty()


class SyncProgress:
    """Context manager that shows progress during sync.

    In stdio transport (interactive TTY): shows rich progress bars.
    In http transport: logs periodic progress messages instead.

    Usage:
        with SyncProgress(transport="stdio") as progress:
            progress.set_metadata_total(98033)
            progress.advance_metadata(150)
            ...
            progress.set_bodies_total(98033)
            progress.advance_bodies(1)
    """

    def __init__(self, transport: str = "stdio") -> None:
        self._use_bars = is_interactive() and transport == "stdio"
        self._progress: Any = None
        self._meta_task: Any = None
        self._body_task: Any = None
        self._meta_count = 0
        self._meta_total = 0
        self._body_count = 0
        self._body_total = 0
        self._log_interval = 1000

    def __enter__(self) -> "SyncProgress":
        if self._use_bars:
            from rich.progress import (
                BarColumn,
                MofNCompleteColumn,
                Progress,
                SpinnerColumn,
                TaskProgressColumn,
                TextColumn,
                TimeElapsedColumn,
                TimeRemainingColumn,
            )

            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.description}"),
                BarColumn(bar_width=40),
                MofNCompleteColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                console=__import__(
                    "rich.console", fromlist=["Console"]
                ).Console(stderr=True),
                refresh_per_second=4,
            )
            self._progress.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._progress is not None:
            self._progress.stop()

    def _log_progress(self, phase: str, count: int, total: int) -> None:
        """Log progress periodically (for non-bar mode)."""
        import logging

        if count % self._log_interval == 0 or count == total:
            pct = int(count / total * 100) if total else 0
            logging.getLogger("email_mcp").info(
                f"{phase}: {count}/{total} ({pct}%)"
            )

    def set_metadata_total(self, total: int) -> None:
        self._meta_total = total
        if self._progress:
            if self._meta_task is None:
                self._meta_task = self._progress.add_task(
                    "Syncing metadata", total=total
                )
            else:
                self._progress.update(self._meta_task, total=total)

    def advance_metadata(self, n: int = 1) -> None:
        self._meta_count += n
        if self._progress and self._meta_task is not None:
            self._progress.advance(self._meta_task, n)
        elif not self._use_bars:
            self._log_progress(
                "Syncing metadata", self._meta_count, self._meta_total
            )

    def metadata_done(self) -> None:
        if self._progress and self._meta_task is not None:
            self._progress.update(self._meta_task, visible=False)

    def set_bodies_total(self, total: int) -> None:
        self._body_total = total
        if self._progress:
            if self._body_task is None:
                self._body_task = self._progress.add_task(
                    "Indexing bodies", total=total
                )
            else:
                self._progress.update(self._body_task, total=total)

    def advance_bodies(self, n: int = 1) -> None:
        self._body_count += n
        if self._progress and self._body_task is not None:
            self._progress.advance(self._body_task, n)
        elif not self._use_bars:
            self._log_progress(
                "Indexing bodies", self._body_count, self._body_total
            )

    def bodies_done(self) -> None:
        if self._progress and self._body_task is not None:
            self._progress.update(self._body_task, visible=False)
