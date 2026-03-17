"""Tests for the ProtonMail event loop."""

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from email_mcp.db import Database, MessageRow
from email_mcp.event_loop import EventLoop


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


@pytest.fixture
def mock_api() -> MagicMock:
    api = MagicMock()
    api.get_latest_event_id = AsyncMock(return_value="event-000")
    api.get_events = AsyncMock(return_value={
        "EventID": "event-001",
        "More": 0,
        "Refresh": 0,
        "Messages": [],
    })
    api.get_labels = AsyncMock(return_value=[
        {"ID": "0", "Name": "Inbox", "Type": 3, "Color": None, "Order": 0},
        {"ID": "6", "Name": "Archive", "Type": 3, "Color": None, "Order": 0},
    ])
    api.get_messages = AsyncMock(return_value=([], 0))
    return api


@pytest.fixture
def loop(db: Database, mock_api: MagicMock) -> EventLoop:
    return EventLoop(db=db, api=mock_api)


def _make_message_event(
    pm_id: str,
    action: int,
    subject: str = "Test",
    label_ids: list[str] | None = None,
    unread: int = 1,
) -> dict:
    msg = {
        "ID": pm_id,
        "Subject": subject,
        "LabelIDs": label_ids or ["0"],
        "Unread": unread,
        "Time": int(time.time()),
        "Sender": {"Name": "Alice", "Address": "alice@example.com"},
        "ToList": [{"Name": "Bob", "Address": "bob@example.com"}],
        "CCList": [],
        "BCCList": [],
        "Size": 1024,
        "NumAttachments": 0,
        "ExternalID": f"<{pm_id}@example.com>",
    }
    event = {"ID": pm_id, "Action": action}
    if action != 0:  # Delete has no Message payload
        event["Message"] = msg
    return event


class TestInitialSetup:
    async def test_initialises_event_id_from_api(self, loop: EventLoop, db: Database) -> None:
        await loop.initialise()
        assert db.sync_state.get("last_event_id") == "event-000"

    async def test_skips_initialise_if_event_id_exists(
        self, loop: EventLoop, db: Database, mock_api: MagicMock
    ) -> None:
        db.sync_state.set("last_event_id", "existing-id")
        await loop.initialise()
        mock_api.get_latest_event_id.assert_not_called()

    async def test_syncs_labels_on_initialise(
        self, loop: EventLoop, db: Database
    ) -> None:
        await loop.initialise()
        labels = db.labels.all()
        names = {lb["name"] for lb in labels}
        assert "Inbox" in names
        assert "Archive" in names


class TestMessageCreate:
    async def test_create_inserts_message(self, loop: EventLoop, db: Database) -> None:
        event = _make_message_event("pm-001", action=1)
        await loop._handle_message_event(event)
        msg = db.messages.get("pm-001")
        assert msg is not None
        assert msg.subject == "Test"
        assert msg.folder == "INBOX"

    async def test_create_sets_unread(self, loop: EventLoop, db: Database) -> None:
        event = _make_message_event("pm-001", action=1, unread=1)
        await loop._handle_message_event(event)
        assert db.messages.get("pm-001").unread is True

    async def test_create_enqueues_body_fetch(
        self, loop: EventLoop, db: Database
    ) -> None:
        loop._enqueue_body_fetch = MagicMock()
        event = _make_message_event("pm-001", action=1)
        await loop._handle_message_event(event)
        loop._enqueue_body_fetch.assert_called_once_with("pm-001")

    async def test_create_derives_folder_from_labels(
        self, loop: EventLoop, db: Database
    ) -> None:
        event = _make_message_event("pm-001", action=1, label_ids=["6"])
        await loop._handle_message_event(event)
        assert db.messages.get("pm-001").folder == "Archive"


class TestMessageDelete:
    async def test_delete_removes_message(self, loop: EventLoop, db: Database) -> None:
        # Insert first
        db.messages.upsert(MessageRow(
            pm_id="pm-001", message_id="<1@ex.com>", subject="X",
            sender_name="A", sender_email="a@ex.com", recipients=[],
            date=int(time.time()), unread=False, label_ids=["0"],
            folder="INBOX", size=0, has_attachments=False, body_indexed=False,
        ))
        event = {"ID": "pm-001", "Action": 0}
        await loop._handle_message_event(event)
        assert db.messages.get("pm-001") is None

    async def test_delete_nonexistent_is_noop(
        self, loop: EventLoop, db: Database
    ) -> None:
        event = {"ID": "does-not-exist", "Action": 0}
        await loop._handle_message_event(event)  # should not raise


class TestMessageUpdate:
    async def test_update_changes_folder(self, loop: EventLoop, db: Database) -> None:
        db.messages.upsert(MessageRow(
            pm_id="pm-001", message_id="<1@ex.com>", subject="X",
            sender_name="A", sender_email="a@ex.com", recipients=[],
            date=int(time.time()), unread=True, label_ids=["0"],
            folder="INBOX", size=0, has_attachments=False, body_indexed=False,
        ))
        event = _make_message_event("pm-001", action=2, label_ids=["6"], unread=0)
        await loop._handle_message_event(event)
        msg = db.messages.get("pm-001")
        assert msg.folder == "Archive"
        assert msg.unread is False

    async def test_update_flags_only(self, loop: EventLoop, db: Database) -> None:
        db.messages.upsert(MessageRow(
            pm_id="pm-001", message_id="<1@ex.com>", subject="X",
            sender_name="A", sender_email="a@ex.com", recipients=[],
            date=int(time.time()), unread=True, label_ids=["0"],
            folder="INBOX", size=0, has_attachments=False, body_indexed=False,
        ))
        event = _make_message_event("pm-001", action=3, label_ids=["0"], unread=0)
        await loop._handle_message_event(event)
        assert db.messages.get("pm-001").unread is False


class TestPollOnce:
    async def test_poll_advances_event_id(
        self, loop: EventLoop, db: Database, mock_api: MagicMock
    ) -> None:
        db.sync_state.set("last_event_id", "event-000")
        mock_api.get_events.return_value = {
            "EventID": "event-001",
            "More": 0,
            "Refresh": 0,
            "Messages": [],
        }
        await loop.poll_once()
        assert db.sync_state.get("last_event_id") == "event-001"

    async def test_poll_processes_events(
        self, loop: EventLoop, db: Database, mock_api: MagicMock
    ) -> None:
        db.sync_state.set("last_event_id", "event-000")
        mock_api.get_events.return_value = {
            "EventID": "event-001",
            "More": 0,
            "Refresh": 0,
            "Messages": [_make_message_event("pm-001", action=1)],
        }
        await loop.poll_once()
        assert db.messages.get("pm-001") is not None

    async def test_poll_fetches_more_when_flagged(
        self, loop: EventLoop, db: Database, mock_api: MagicMock
    ) -> None:
        db.sync_state.set("last_event_id", "event-000")
        mock_api.get_events.side_effect = [
            {"EventID": "event-001", "More": 1, "Refresh": 0, "Messages": []},
            {"EventID": "event-002", "More": 0, "Refresh": 0, "Messages": []},
        ]
        await loop.poll_once()
        assert mock_api.get_events.call_count == 2
        assert db.sync_state.get("last_event_id") == "event-002"

    async def test_refresh_triggers_full_resync(
        self, loop: EventLoop, db: Database, mock_api: MagicMock
    ) -> None:
        db.sync_state.set("last_event_id", "event-000")
        mock_api.get_events.return_value = {
            "EventID": "event-001",
            "More": 0,
            "Refresh": 1,
            "Messages": [],
        }
        loop.full_resync = AsyncMock()
        await loop.poll_once()
        loop.full_resync.assert_called_once()
