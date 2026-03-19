"""Background content indexer for v4 architecture.

Fetches message content from the ProtonMail API — one get_message() call per
message, extracting whatever is needed:

  - Body: decrypt via ProtonDecryptor, index plaintext into SQLite FTS5
  - Headers: store ParsedHeaders JSON for bulk email detection
  - Attachments: store attachment metadata

Two modes:
  1. Queue-driven (ongoing): consumes pm_ids from an asyncio.Queue.
  2. Bulk (initial sync / catchup): queries DB for all unindexed pm_ids.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

from email_mcp.db import Database
from email_mcp.decryptor import ProtonDecryptor

logger = structlog.get_logger(__name__)

_BATCH_SIZE = 200


class BodyIndexer:
    """Fetches and indexes message content into SQLite."""

    retry_delays: list[int] = [5, 15, 60]  # seconds between retries

    def __init__(
        self, db: Database, decryptor: ProtonDecryptor, workers: int = 3, progress: Any = None
    ) -> None:
        self._db = db
        self._decryptor = decryptor
        self._workers = workers
        self._progress = progress

    # ── Single message ────────────────────────────────────────────────────────

    async def _fetch_and_index(self, pm_id: str) -> None:
        """Fetch content for one pm_id and index it. Safe to call redundantly."""
        row = self._db.messages.get(pm_id)
        if row is None:
            return
        if row.body_indexed:
            return

        delays = self.retry_delays
        for attempt, delay in enumerate(delays + [None]):
            try:
                body, attachments, parsed_headers = await self._decryptor.fetch_and_decrypt(pm_id)
                self._db.bodies.insert(pm_id, body)
                if attachments:
                    self._db.attachments.upsert_for_message(pm_id, attachments)
                if parsed_headers is not None:
                    self._store_headers(pm_id, parsed_headers)
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
                        "UPDATE messages SET body_indexed = -1 WHERE pm_id = ?",
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
            rows = self._db.execute("SELECT pm_id FROM messages WHERE body_indexed = 0").fetchall()
        elif folder is None:
            rows = self._db.execute(
                "SELECT pm_id FROM messages WHERE body_indexed = 0 AND folder IS NULL"
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT pm_id FROM messages WHERE body_indexed = 0 AND folder = ?",
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

            for pm_id, (body, attachments, parsed_headers) in results.items():
                self._db.bodies.insert(pm_id, body)
                if attachments:
                    self._db.attachments.upsert_for_message(pm_id, attachments)
                if parsed_headers is not None:
                    self._store_headers(pm_id, parsed_headers)
                self._db.messages.mark_body_indexed(pm_id)
                indexed += 1
                if self._progress:
                    self._progress.advance_bodies()

            # Mark failures as -1 so they aren't retried forever
            failed_ids = set(batch) - set(results.keys())
            for pm_id in failed_ids:
                self._db.execute(
                    "UPDATE messages SET body_indexed = -1 WHERE pm_id = ?",
                    [pm_id],
                )
            if failed_ids:
                self._db.commit()
            failed += len(failed_ids)

        logger.info(
            "body_indexer.index_unindexed.done",
            folder=folder,
            indexed=indexed,
            failed=failed,
            total=len(pm_ids),
        )

    # ── Content re-index ──────────────────────────────────────────────────────

    def _store_headers(self, pm_id: str, parsed_headers: dict[str, Any]) -> None:
        """Store ParsedHeaders JSON and mark headers_indexed."""
        self._db.execute(
            "UPDATE messages SET parsed_headers = ?, headers_indexed = 1 WHERE pm_id = ?",
            [json.dumps(parsed_headers), pm_id],
        )
        self._db.commit()

    async def reindex_content(self, *, bodies: bool = False, headers: bool = False) -> None:
        """Re-index content for messages that need it.

        One get_message() call per message — extracts bodies, headers, or both.
        Skips work that's already done unless the corresponding flag is set.

        Args:
            bodies: Re-index bodies (decrypt + store).
            headers: Re-index headers (store ParsedHeaders JSON).
        """
        if not bodies and not headers:
            return

        # Find messages that need work
        conditions = []
        if bodies:
            conditions.append("body_indexed = 0")
        if headers:
            conditions.append("headers_indexed = 0")
        where = " OR ".join(conditions)

        rows = self._db.execute(
            f"SELECT pm_id, body_indexed, headers_indexed FROM messages WHERE {where}"
        ).fetchall()

        if not rows:
            logger.info("body_indexer.reindex_content.empty", bodies=bodies, headers=headers)
            return

        logger.info(
            "body_indexer.reindex_content.start",
            count=len(rows),
            bodies=bodies,
            headers=headers,
        )
        indexed = 0
        failed = 0

        pm_ids = [r[0] for r in rows]
        # Track what each message needs
        needs_body = {r[0] for r in rows if bodies and r[1] == 0}
        needs_headers = {r[0] for r in rows if headers and r[2] == 0}

        for i in range(0, len(pm_ids), _BATCH_SIZE):
            batch = pm_ids[i : i + _BATCH_SIZE]

            # Messages needing body decryption go through full fetch_and_decrypt
            batch_need_body = [pid for pid in batch if pid in needs_body]
            batch_headers_only = [pid for pid in batch if pid not in needs_body]

            # Full decrypt for messages needing bodies (also gets headers)
            if batch_need_body:
                results = await self._decryptor.fetch_and_decrypt_batch(batch_need_body)
                for pm_id, (body, attachments, parsed_headers) in results.items():
                    self._db.bodies.insert(pm_id, body)
                    if attachments:
                        self._db.attachments.upsert_for_message(pm_id, attachments)
                    if parsed_headers is not None and pm_id in needs_headers:
                        self._store_headers(pm_id, parsed_headers)
                    self._db.messages.mark_body_indexed(pm_id)
                    indexed += 1
                failed += len(batch_need_body) - len(results)

            # Headers-only for messages that already have bodies
            if batch_headers_only:
                results = await self._decryptor.fetch_headers_batch(batch_headers_only)
                for pm_id, hdrs in results.items():
                    self._store_headers(pm_id, hdrs)
                    indexed += 1
                failed += len(batch_headers_only) - len(results)

        logger.info(
            "body_indexer.reindex_content.done",
            indexed=indexed,
            failed=failed,
            total=len(pm_ids),
        )
