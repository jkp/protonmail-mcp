"""Interactive progress display for initial sync and body indexing.

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
    """Context manager that shows rich progress bars during initial sync.

    Usage:
        with SyncProgress() as progress:
            progress.set_metadata_total(98033)
            progress.advance_metadata(150)
            ...
            progress.set_bodies_total(98033)
            progress.advance_bodies(1)
    """

    def __init__(self) -> None:
        self._interactive = is_interactive()
        self._progress: Any = None
        self._meta_task: Any = None
        self._body_task: Any = None

    def __enter__(self) -> "SyncProgress":
        if self._interactive:
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
                refresh_per_second=4,
            )
            self._progress.start()
            self._meta_task = self._progress.add_task("Syncing metadata ", total=None)
            self._body_task = self._progress.add_task("Indexing bodies  ", total=None, visible=False)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._progress is not None:
            self._progress.stop()

    def set_metadata_total(self, total: int) -> None:
        if self._progress and self._meta_task is not None:
            self._progress.update(self._meta_task, total=total)

    def advance_metadata(self, n: int = 1) -> None:
        if self._progress and self._meta_task is not None:
            self._progress.advance(self._meta_task, n)

    def metadata_done(self) -> None:
        if self._progress and self._meta_task is not None:
            self._progress.update(self._meta_task, description="Metadata synced  ")

    def set_bodies_total(self, total: int) -> None:
        if self._progress and self._body_task is not None:
            self._progress.update(self._body_task, total=total, visible=True)

    def advance_bodies(self, n: int = 1) -> None:
        if self._progress and self._body_task is not None:
            self._progress.advance(self._body_task, n)

    def bodies_done(self) -> None:
        if self._progress and self._body_task is not None:
            self._progress.update(self._body_task, description="Bodies indexed   ")
