"""Background body indexer for v4 architecture.

Fetches encrypted message bodies from the ProtonMail API, decrypts them
via ProtonDecryptor, and indexes plaintext into SQLite FTS5.

Two modes:
  1. Queue-driven (ongoing): consumes pm_ids from an asyncio.Queue,
     fetches and decrypts each body via the API.

  2. Bulk (initial sync / catchup): queries DB for all unindexed pm_ids,
     fetches and decrypts in parallel batches.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from email_mcp.db import Database
from email_mcp.decryptor import ProtonDecryptor

logger = structlog.get_logger(__name__)

_BATCH_SIZE = 200


class BodyIndexer:
    """Fetches and indexes message bodies into SQLite FTS5."""

    retry_delays: list[int] = [5, 15, 60]  # seconds between retries

    def __init__(self, db: Database, decryptor: ProtonDecryptor, workers: int = 3, progress: Any = None) -> None:
        self._db = db
        self._decryptor = decryptor
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

        delays = self.retry_delays
        for attempt, delay in enumerate(delays + [None]):
            try:
                body, attachments = await self._decryptor.fetch_and_decrypt(pm_id)
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
                    logger.warning(
                        "body_indexer.fetch_failed",
                        pm_id=pm_id,
                        error=str(e),
                        attempts=attempt + 1,
                    )
                    # Mark as permanently failed (-1) so we don't retry forever
                    self._db.execute(
                        "UPDATE messages SET body_indexed = -1"
                        " WHERE pm_id = ?",
                        [pm_id],
                    )
                    self._db.commit()
                else:
                    logger.debug(
                        "body_indexer.fetch_retry",
                        pm_id=pm_id,
                        attempt=attempt + 1,
                        retry_in=delay,
                    )
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

    # ── Bulk index (initial sync / catchup) ──────────────────────────────────

    _ALL = object()  # sentinel for "all folders"

    async def index_unindexed(self, folder: str | None | object = _ALL) -> None:
        """Bulk-fetch and decrypt all unindexed message bodies.

        Args:
            folder: Folder to index. Specific string = that folder.
                    None = messages with NULL folder. _ALL (default) = everything.
        """
        if folder is self._ALL:
            rows = self._db.execute(
                "SELECT pm_id FROM messages WHERE body_indexed = 0"
            ).fetchall()
        elif folder is None:
            rows = self._db.execute(
                "SELECT pm_id FROM messages"
                " WHERE body_indexed = 0 AND folder IS NULL"
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT pm_id FROM messages"
                " WHERE body_indexed = 0 AND folder = ?",
                [folder],
            ).fetchall()

        pm_ids = [r[0] for r in rows]
        if not pm_ids:
            logger.info("body_indexer.index_unindexed.empty", folder=folder)
            return

        logger.info("body_indexer.index_unindexed.start", folder=folder, count=len(pm_ids))
        indexed = 0
        failed = 0

        # Process in batches to avoid overwhelming the API
        for i in range(0, len(pm_ids), _BATCH_SIZE):
            batch = pm_ids[i : i + _BATCH_SIZE]
            results = await self._decryptor.fetch_and_decrypt_batch(batch)

            for pm_id, (body, attachments) in results.items():
                self._db.bodies.insert(pm_id, body)
                if attachments:
                    self._db.attachments.upsert_for_message(pm_id, attachments)
                self._db.messages.mark_body_indexed(pm_id)
                indexed += 1
                if self._progress:
                    self._progress.advance_bodies()

            batch_failed = len(batch) - len(results)
            failed += batch_failed

        logger.info("body_indexer.index_unindexed.done", folder=folder, indexed=indexed, failed=failed, total=len(pm_ids))
