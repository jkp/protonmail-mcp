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

    def __init__(self, db: Database, imap: Any, workers: int = 3) -> None:
        self._db = db
        self._imap = imap
        self._workers = workers

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

        try:
            body = await self._imap.fetch_body(row.message_id, folder=row.folder)
            self._db.bodies.insert(pm_id, body)
            self._db.messages.mark_body_indexed(pm_id)
            logger.debug("body_indexer.indexed", pm_id=pm_id)
        except Exception as e:
            logger.warning("body_indexer.fetch_failed", pm_id=pm_id, error=str(e))

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

        # Build a reverse index: message_id → pm_id
        mid_to_pmid: dict[str, str] = {}
        rows = self._db.execute(
            "SELECT pm_id, message_id FROM messages WHERE message_id IS NOT NULL"
        ).fetchall()
        for row in rows:
            mid_to_pmid[row[0]] = row[1]  # message_id → pm_id... wait, reversed

        # message_id → pm_id
        mid_to_pmid = {}
        for row in self._db.execute(
            "SELECT pm_id, message_id FROM messages WHERE message_id IS NOT NULL"
        ).fetchall():
            mid_to_pmid[row[1]] = row[0]  # message_id → pm_id

        indexed = 0
        for message_id, body in bodies.items():
            pm_id = mid_to_pmid.get(message_id)
            if not pm_id:
                continue
            self._db.bodies.insert(pm_id, body)
            self._db.messages.mark_body_indexed(pm_id)
            indexed += 1

        logger.info("body_indexer.index_folder.done", folder=folder, indexed=indexed, fetched=len(bodies))
