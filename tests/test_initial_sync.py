"""Tests for the initial sync orchestrator."""

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from email_mcp.db import Database
from email_mcp.initial_sync import InitialSync


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


def _make_api_message(pm_id: str, label_ids: list[str] | None = None) -> dict:
    return {
        "ID": pm_id,
        "Subject": f"Subject {pm_id}",
        "LabelIDs": label_ids or ["0"],
        "Unread": 1,
        "Time": int(time.time()),
        "Sender": {"Name": "Alice", "Address": "alice@example.com"},
        "ToList": [{"Name": "Bob", "Address": "bob@example.com"}],
        "CCList": [],
        "BCCList": [],
        "Size": 512,
        "NumAttachments": 0,
        "ExternalID": f"<{pm_id}@example.com>",
    }


@pytest.fixture
def mock_api() -> MagicMock:
    api = MagicMock()
    api.get_labels = AsyncMock(return_value=[
        {"ID": "0", "Name": "Inbox", "Type": 3, "Color": None, "Order": 0},
        {"ID": "6", "Name": "Archive", "Type": 3, "Color": None, "Order": 0},
    ])
    api.get_messages = AsyncMock(return_value=(
        [_make_api_message("pm-001"), _make_api_message("pm-002")],
        2,
    ))
    api.get_latest_event_id = AsyncMock(return_value="event-start")
    return api


@pytest.fixture
def mock_body_indexer() -> MagicMock:
    indexer = MagicMock()
    indexer.index_folder = AsyncMock()
    return indexer


@pytest.fixture
def sync(db: Database, mock_api: MagicMock, mock_body_indexer: MagicMock) -> InitialSync:
    return InitialSync(db=db, api=mock_api, body_indexer=mock_body_indexer)


class TestInitialSync:
    async def test_skips_if_already_done(
        self, sync: InitialSync, db: Database, mock_api: MagicMock
    ) -> None:
        db.sync_state.set("initial_sync_done", "1")
        await sync.run()
        mock_api.get_messages.assert_not_called()

    async def test_fetches_labels(
        self, sync: InitialSync, db: Database, mock_api: MagicMock
    ) -> None:
        await sync.run()
        mock_api.get_labels.assert_called_once()
        labels = db.labels.all()
        assert any(lb["name"] == "Inbox" for lb in labels)

    async def test_fetches_all_message_pages(
        self, sync: InitialSync, db: Database, mock_api: MagicMock
    ) -> None:
        await sync.run()
        mock_api.get_messages.assert_called()
        assert db.messages.get("pm-001") is not None
        assert db.messages.get("pm-002") is not None

    async def test_paginates_until_all_fetched(
        self, sync: InitialSync, db: Database, mock_api: MagicMock
    ) -> None:
        # 3 messages, page_size=2: needs 2 pages
        mock_api.get_messages.side_effect = [
            ([_make_api_message("pm-001"), _make_api_message("pm-002")], 3),
            ([_make_api_message("pm-003")], 3),
        ]
        await sync.run()
        assert db.messages.get("pm-001") is not None
        assert db.messages.get("pm-003") is not None
        assert mock_api.get_messages.call_count == 2

    async def test_marks_done_after_completion(
        self, sync: InitialSync, db: Database
    ) -> None:
        await sync.run()
        assert db.sync_state.get("initial_sync_done") == "1"

    async def test_stores_event_id_before_fetching(
        self, sync: InitialSync, db: Database, mock_api: MagicMock
    ) -> None:
        """Event ID must be captured BEFORE we start fetching metadata,
        so we don't miss events that arrive during the initial sync."""
        await sync.run()
        assert db.sync_state.get("last_event_id") == "event-start"
        # event ID should be fetched before messages
        assert mock_api.get_latest_event_id.call_count == 1

    async def test_marks_done_after_metadata_sync(
        self, sync: InitialSync, db: Database, mock_body_indexer: MagicMock
    ) -> None:
        await sync.run()
        # Body indexing is now handled by server.py bulk reindex, not initial_sync
        assert db.sync_state.get("initial_sync_done") == "1"

    async def test_does_not_mark_done_on_error(
        self, sync: InitialSync, db: Database, mock_api: MagicMock
    ) -> None:
        mock_api.get_messages.side_effect = Exception("API down")
        with pytest.raises(Exception, match="API down"):
            await sync.run()
        assert db.sync_state.get("initial_sync_done") is None
