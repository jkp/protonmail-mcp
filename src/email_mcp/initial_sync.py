"""Initial sync orchestrator for v4 architecture.

On first start (or after a Refresh=1 event), fetches all message metadata
from the ProtonMail API and queues body fetches per folder.

Critically, the latest EventID is captured BEFORE fetching metadata so
that events arriving during the sync are not missed.
"""

from __future__ import annotations

from typing import Any

import structlog

from email_mcp.db import Database
from email_mcp.event_loop import _event_to_row
from email_mcp.proton_api import ProtonClient, derive_folder

logger = structlog.get_logger(__name__)

_PAGE_SIZE = 150


class InitialSync:
    """Fetches all ProtonMail message metadata into SQLite on first start."""

    def __init__(
        self,
        db: Database,
        api: ProtonClient,
        body_indexer: Any,
    ) -> None:
        self._db = db
        self._api = api
        self._body_indexer = body_indexer

    async def run(self) -> None:
        """Run the initial sync. No-op if already completed."""
        if self._db.sync_state.get("initial_sync_done") == "1":
            logger.info("initial_sync.already_done")
            return

        logger.info("initial_sync.start")

        # Capture event ID FIRST — so we don't miss events during the sync
        event_id = await self._api.get_latest_event_id()
        self._db.sync_state.set("last_event_id", event_id)
        logger.info("initial_sync.event_id_anchored", event_id=event_id)

        # Sync labels
        labels = await self._api.get_labels()
        for label in labels:
            self._db.labels.upsert(
                id=label["ID"],
                name=label["Name"],
                type=label.get("Type", 1),
                color=label.get("Color"),
                order=label.get("Order"),
            )
        logger.info("initial_sync.labels_done", count=len(labels))

        # Fetch all message metadata, paginated
        folders_seen: set[str] = set()
        page = 0
        synced = 0

        while True:
            messages, total = await self._api.get_messages(
                page=page, page_size=_PAGE_SIZE
            )
            if not messages:
                break

            for msg in messages:
                pm_id = msg["ID"]
                label_ids = msg.get("LabelIDs", [])
                folder = derive_folder(label_ids)
                row = _event_to_row(pm_id, msg, folder)
                self._db.messages.upsert(row)
                if folder:
                    folders_seen.add(folder)

            synced += len(messages)
            logger.info("initial_sync.messages_progress", synced=synced, total=total)

            if synced >= total:
                break
            page += 1

        logger.info("initial_sync.metadata_done", synced=synced)

        # Kick off body indexing per folder (background, best-effort)
        for folder in folders_seen:
            await self._body_indexer.index_folder(folder)

        self._db.sync_state.set("initial_sync_done", "1")
        logger.info("initial_sync.done", folders=len(folders_seen))
