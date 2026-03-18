"""Background body indexer for v4 architecture.

Fetches decrypted message bodies via Bridge IMAP and indexes them into
SQLite FTS5. Two modes:

  1. Queue-driven (ongoing): consumes pm_ids from an asyncio.Queue,
     fetches each body individually via IMAP SEARCH + FETCH.

  2. Bulk folder (initial sync): fetches all bodies in a folder with
     a single chunked IMAP FETCH command, correlates by Message-ID.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from email_mcp.db import Database

logger = structlog.get_logger(__name__)


class BodyIndexer:
    """Fetches and indexes message bodies from Bridge IMAP into SQLite FTS5."""

    def __init__(self, db: Database, imap: Any, workers: int = 3, progress: Any = None) -> None:
        self._db = db
        self._imap = imap
        self._workers = workers
        self._progress = progress

    # ── Single message ────────────────────────────────────────────────────────

    async def _fetch_and_index(self, pm_id: str) -> None:
        """Fetch body for one pm_id and index it. Safe to call redundantly."""
        row = self._db.messages.get(pm_id)
        if row is None:
            return
        if row.body_indexed:
            return
        if not row.message_id:
            logger.warning("body_indexer.no_message_id", pm_id=pm_id)
            return

        delays = [5, 15, 60]  # seconds between retries
        for attempt, delay in enumerate(delays + [None]):
            try:
                body, attachments = await self._imap.fetch_body_and_structure(
                    row.message_id, folder=row.folder
                )
                self._db.bodies.insert(pm_id, body)
                if attachments:
                    self._db.attachments.upsert_for_message(pm_id, attachments)
                self._db.messages.mark_body_indexed(pm_id)
                if self._progress:
                    self._progress.advance_bodies()
                logger.debug("body_indexer.indexed", pm_id=pm_id, attachments=len(attachments))
                return
            except Exception as e:
                if delay is None:
                    logger.warning("body_indexer.fetch_failed", pm_id=pm_id, error=str(e), attempts=attempt + 1)
                else:
                    logger.debug("body_indexer.fetch_retry", pm_id=pm_id, attempt=attempt + 1, retry_in=delay)
                    await asyncio.sleep(delay)

    # ── Queue worker ──────────────────────────────────────────────────────────

    async def run_queue(self, queue: asyncio.Queue) -> None:
        """Drain queue until a None sentinel is received."""
        while True:
            pm_id = await queue.get()
            if pm_id is None:
                queue.task_done()
                break
            try:
                await self._fetch_and_index(pm_id)
            finally:
                queue.task_done()

    async def run_workers(self, queue: asyncio.Queue) -> None:
        """Run N concurrent workers draining the queue."""
        tasks = [asyncio.create_task(self.run_queue(queue)) for _ in range(self._workers)]
        await asyncio.gather(*tasks)

    # ── Bulk folder index (initial sync) ──────────────────────────────────────

    async def index_folder(self, folder: str) -> None:
        """Bulk-fetch all bodies in a folder and index them.

        Correlates IMAP bodies to SQLite rows by RFC 2822 Message-ID.
        One IMAP command per 200-message chunk — efficient for initial import.
        """
        logger.info("body_indexer.index_folder.start", folder=folder)
        bodies = await self._imap.fetch_bodies_in_folder(folder)

        if not bodies:
            logger.info("body_indexer.index_folder.empty", folder=folder)
            return

        # message_id → [pm_id, ...] (duplicates share the same body)
        mid_to_pmids: dict[str, list[str]] = {}
        for row in self._db.execute(
            "SELECT pm_id, message_id FROM messages WHERE message_id IS NOT NULL"
        ).fetchall():
            mid_to_pmids.setdefault(row[1], []).append(row[0])

        indexed = 0
        for message_id, (body, attachments) in bodies.items():
            pm_ids = mid_to_pmids.get(message_id)
            if not pm_ids:
                continue
            for pm_id in pm_ids:
                self._db.bodies.insert(pm_id, body)
                if attachments:
                    self._db.attachments.upsert_for_message(pm_id, attachments)
                self._db.messages.mark_body_indexed(pm_id)
                indexed += 1

        logger.info("body_indexer.index_folder.done", folder=folder, indexed=indexed, fetched=len(bodies), duplicates=indexed - len(bodies) if indexed > len(bodies) else 0)
