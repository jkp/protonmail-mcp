"""ProtonMail event loop — real-time sync from ProtonMail's event API.

Polls /mail/v4/events/{last_event_id} every 30s. Handles:
  - Message create / update / delete
  - Label changes (moves)
  - Refresh=1 (full resync when event cursor is too old)

Action values from ProtonMail API:
  0 = Delete
  1 = Create
  2 = Update (metadata + labels)
  3 = UpdateFlags (read/unread only)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import structlog

from email_mcp.db import Database, MessageRow
from email_mcp.proton_api import ProtonClient, RateLimitError, derive_folder

logger = structlog.get_logger(__name__)

_ACTION_DELETE = 0
_ACTION_CREATE = 1
_ACTION_UPDATE = 2
_ACTION_UPDATE_FLAGS = 3


class EventLoop:
    """Polls the ProtonMail event API and keeps SQLite up to date."""

    def __init__(
        self,
        db: Database,
        api: ProtonClient,
        poll_interval: float = 30.0,
    ) -> None:
        self._db = db
        self._api = api
        self._poll_interval = poll_interval
        self._running = False
        self._body_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)

    # ── Public interface ──────────────────────────────────────────────────────

    async def initialise(self) -> None:
        """Set up event cursor and sync labels. Idempotent."""
        if not self._db.sync_state.get("last_event_id"):
            event_id = await self._api.get_latest_event_id()
            self._db.sync_state.set("last_event_id", event_id)
            logger.info("event_loop.initialised", event_id=event_id)

        await self._sync_labels()

    async def run(self) -> None:
        """Run the event loop forever. Call from an asyncio task."""
        self._running = True
        logger.info("event_loop.started", poll_interval=self._poll_interval)
        while self._running:
            try:
                await self.poll_once()
            except RateLimitError as e:
                logger.warning("event_loop.rate_limited", retry_after=e.retry_after)
                await asyncio.sleep(e.retry_after)
            except Exception as e:
                logger.error("event_loop.error", error=str(e))
                await asyncio.sleep(self._poll_interval)
            else:
                await asyncio.sleep(self._poll_interval)

    def stop(self) -> None:
        self._running = False

    # ── Polling ───────────────────────────────────────────────────────────────

    async def poll_once(self) -> None:
        """Fetch and process one batch of events (follows More=1 automatically)."""
        last_id = self._db.sync_state.get("last_event_id")
        if not last_id:
            await self.initialise()
            last_id = self._db.sync_state.get("last_event_id")

        while True:
            data = await self._api.get_events(last_id)

            if data.get("Refresh", 0) & 1:
                logger.warning("event_loop.refresh_required")
                await self.full_resync()

            for event in data.get("Messages", []):
                await self._handle_message_event(event)

            for event in data.get("Labels", []):
                await self._handle_label_event(event)

            last_id = data["EventID"]
            self._db.sync_state.set("last_event_id", last_id)

            if not data.get("More", 0):
                break

    # ── Event handlers ────────────────────────────────────────────────────────

    async def _handle_message_event(self, event: dict[str, Any]) -> None:
        pm_id = event["ID"]
        action = event["Action"]

        if action == _ACTION_DELETE:
            self._db.messages.delete(pm_id)
            logger.debug("event_loop.message_deleted", pm_id=pm_id)
            return

        msg = event.get("Message", {})
        label_ids: list[str] = msg.get("LabelIDs", [])
        folder = derive_folder(label_ids)

        if action == _ACTION_CREATE:
            row = _event_to_row(pm_id, msg, folder)
            self._db.messages.upsert(row)
            self._enqueue_body_fetch(pm_id)
            logger.debug("event_loop.message_created", pm_id=pm_id, folder=folder)

        elif action in (_ACTION_UPDATE, _ACTION_UPDATE_FLAGS):
            existing = self._db.messages.get(pm_id)
            if existing:
                self._db.messages.update_folder(pm_id, folder or existing.folder, label_ids)
                # Also update unread flag
                self._db.execute(
                    "UPDATE messages SET unread = ?, updated_at = unixepoch() WHERE pm_id = ?",
                    [int(msg.get("Unread", existing.unread)), pm_id],
                )
                self._db.execute("COMMIT") if False else None  # already auto-committed
                logger.debug("event_loop.message_updated", pm_id=pm_id, folder=folder)
            else:
                # Message not in local DB — treat as create
                row = _event_to_row(pm_id, msg, folder)
                self._db.messages.upsert(row)
                self._enqueue_body_fetch(pm_id)

    async def _handle_label_event(self, event: dict[str, Any]) -> None:
        action = event["Action"]
        if action == _ACTION_DELETE:
            label_id = event["ID"]
            self._db.execute("DELETE FROM labels WHERE id = ?", [label_id])
            self._db.execute("COMMIT") if False else None
        else:
            label = event.get("Label", {})
            self._db.labels.upsert(
                id=label.get("ID", event["ID"]),
                name=label.get("Name", ""),
                type=label.get("Type", 1),
                color=label.get("Color"),
                order=label.get("Order"),
            )

    # ── Full resync ───────────────────────────────────────────────────────────

    async def full_resync(self) -> None:
        """Resync all message metadata from the API. Called on Refresh=1."""
        logger.info("event_loop.full_resync.start")
        await self._sync_labels()

        page = 0
        page_size = 150
        synced = 0

        while True:
            messages, total = await self._api.get_messages(page=page, page_size=page_size)
            if not messages:
                break

            for msg in messages:
                pm_id = msg["ID"]
                label_ids = msg.get("LabelIDs", [])
                folder = derive_folder(label_ids)
                row = _event_to_row(pm_id, msg, folder)
                self._db.messages.upsert(row)
                if not row.body_indexed:
                    self._enqueue_body_fetch(pm_id)

            synced += len(messages)
            logger.info("event_loop.full_resync.progress", synced=synced, total=total)

            if synced >= total:
                break
            page += 1

        logger.info("event_loop.full_resync.done", synced=synced)

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _sync_labels(self) -> None:
        labels = await self._api.get_labels()
        for label in labels:
            self._db.labels.upsert(
                id=label["ID"],
                name=label["Name"],
                type=label.get("Type", 1),
                color=label.get("Color"),
                order=label.get("Order"),
            )
        logger.debug("event_loop.labels_synced", count=len(labels))

    def _enqueue_body_fetch(self, pm_id: str) -> None:
        try:
            self._body_queue.put_nowait(pm_id)
        except asyncio.QueueFull:
            logger.warning("event_loop.body_queue_full", pm_id=pm_id)

    @property
    def body_queue(self) -> asyncio.Queue[str]:
        return self._body_queue


# ── Helpers ───────────────────────────────────────────────────────────────────

def _event_to_row(pm_id: str, msg: dict[str, Any], folder: str | None) -> MessageRow:
    sender = msg.get("Sender", {})
    recipients = [
        {"name": r.get("Name", ""), "email": r.get("Address", "")}
        for r in msg.get("ToList", []) + msg.get("CCList", []) + msg.get("BCCList", [])
    ]
    return MessageRow(
        pm_id=pm_id,
        message_id=(msg.get("ExternalID") or f"{pm_id}@protonmail.com").strip().strip("<>"),
        subject=msg.get("Subject"),
        sender_name=sender.get("Name"),
        sender_email=sender.get("Address"),
        recipients=recipients,
        date=msg.get("Time", 0),
        unread=bool(msg.get("Unread", 1)),
        label_ids=msg.get("LabelIDs", []),
        folder=folder,
        size=msg.get("Size"),
        has_attachments=bool(msg.get("NumAttachments", 0)),
        body_indexed=False,
    )
