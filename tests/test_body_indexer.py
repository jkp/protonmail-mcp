"""Tests for the background body indexer."""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from email_mcp.body_indexer import BodyIndexer
from email_mcp.db import Database, MessageRow


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


def _insert_message(db: Database, pm_id: str, message_id: str, body_indexed: bool = False) -> None:
    db.messages.upsert(MessageRow(
        pm_id=pm_id,
        message_id=message_id,
        subject="Test",
        sender_name="Alice",
        sender_email="alice@example.com",
        recipients=[],
        date=int(time.time()),
        unread=True,
        label_ids=["0"],
        folder="INBOX",
        size=1024,
        has_attachments=False,
        body_indexed=body_indexed,
    ))


@pytest.fixture
def mock_imap() -> MagicMock:
    imap = MagicMock()
    imap.fetch_body_and_structure = AsyncMock(return_value=("Hello, this is the email body.", []))
    imap.fetch_bodies_in_folder = AsyncMock(return_value={
        "<msg1@example.com>": ("Body of message 1", []),
        "<msg2@example.com>": ("Body of message 2", []),
    })
    return imap


@pytest.fixture
def indexer(db: Database, mock_imap: MagicMock) -> BodyIndexer:
    return BodyIndexer(db=db, imap=mock_imap, workers=2)


class TestSingleFetch:
    async def test_indexes_body_for_queued_message(
        self, indexer: BodyIndexer, db: Database
    ) -> None:
        _insert_message(db, "pm-001", "<msg1@example.com>")
        await indexer._fetch_and_index("pm-001")
        body = db.bodies.get("pm-001")
        assert body == "Hello, this is the email body."

    async def test_marks_body_indexed(
        self, indexer: BodyIndexer, db: Database
    ) -> None:
        _insert_message(db, "pm-001", "<msg1@example.com>")
        await indexer._fetch_and_index("pm-001")
        assert db.messages.get("pm-001").body_indexed is True

    async def test_skips_already_indexed(
        self, indexer: BodyIndexer, db: Database, mock_imap: MagicMock
    ) -> None:
        _insert_message(db, "pm-001", "<msg1@example.com>", body_indexed=True)
        await indexer._fetch_and_index("pm-001")
        mock_imap.fetch_body_and_structure.assert_not_called()

    async def test_handles_missing_message_id(
        self, indexer: BodyIndexer, db: Database, mock_imap: MagicMock
    ) -> None:
        """Message with no RFC2822 message_id — skip gracefully."""
        db.messages.upsert(MessageRow(
            pm_id="pm-001", message_id=None, subject="X",
            sender_name="A", sender_email="a@ex.com", recipients=[],
            date=int(time.time()), unread=False, label_ids=["0"],
            folder="INBOX", size=0, has_attachments=False, body_indexed=False,
        ))
        await indexer._fetch_and_index("pm-001")
        mock_imap.fetch_body_and_structure.assert_not_called()

    async def test_handles_imap_fetch_error(
        self, indexer: BodyIndexer, db: Database, mock_imap: MagicMock
    ) -> None:
        _insert_message(db, "pm-001", "<msg1@example.com>")
        mock_imap.fetch_body_and_structure.side_effect = Exception("IMAP error")
        await indexer._fetch_and_index("pm-001")  # should not raise
        assert db.messages.get("pm-001").body_indexed is False

    async def test_unknown_pm_id_is_noop(
        self, indexer: BodyIndexer, db: Database, mock_imap: MagicMock
    ) -> None:
        await indexer._fetch_and_index("does-not-exist")
        mock_imap.fetch_body_and_structure.assert_not_called()


class TestQueueProcessing:
    async def test_processes_queued_items(
        self, indexer: BodyIndexer, db: Database
    ) -> None:
        _insert_message(db, "pm-001", "<msg1@example.com>")
        _insert_message(db, "pm-002", "<msg2@example.com>")

        queue: asyncio.Queue[str] = asyncio.Queue()
        queue.put_nowait("pm-001")
        queue.put_nowait("pm-002")
        queue.put_nowait(None)  # sentinel

        await indexer.run_queue(queue)

        assert db.messages.get("pm-001").body_indexed is True
        assert db.messages.get("pm-002").body_indexed is True

    async def test_stops_on_sentinel(
        self, indexer: BodyIndexer, db: Database
    ) -> None:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        queue.put_nowait(None)
        # Should return without hanging
        await asyncio.wait_for(indexer.run_queue(queue), timeout=1.0)


class TestBulkFolderIndex:
    async def test_indexes_all_messages_in_folder(
        self, indexer: BodyIndexer, db: Database, mock_imap: MagicMock
    ) -> None:
        _insert_message(db, "pm-001", "<msg1@example.com>")
        _insert_message(db, "pm-002", "<msg2@example.com>")

        await indexer.index_folder("INBOX")

        assert db.bodies.get("pm-001") == "Body of message 1"
        assert db.bodies.get("pm-002") == "Body of message 2"

    async def test_bulk_fetch_called_with_folder(
        self, indexer: BodyIndexer, db: Database, mock_imap: MagicMock
    ) -> None:
        _insert_message(db, "pm-001", "<msg1@example.com>")
        await indexer.index_folder("INBOX")
        mock_imap.fetch_bodies_in_folder.assert_called_once_with("INBOX")

    async def test_marks_indexed_after_bulk(
        self, indexer: BodyIndexer, db: Database
    ) -> None:
        _insert_message(db, "pm-001", "<msg1@example.com>")
        _insert_message(db, "pm-002", "<msg2@example.com>")
        await indexer.index_folder("INBOX")
        assert db.messages.get("pm-001").body_indexed is True
        assert db.messages.get("pm-002").body_indexed is True

    async def test_skips_unmatched_message_ids(
        self, indexer: BodyIndexer, db: Database, mock_imap: MagicMock
    ) -> None:
        """Bodies returned by IMAP that don't match any pm_id are ignored."""
        mock_imap.fetch_bodies_in_folder.return_value = {
            "<unknown@example.com>": ("orphan body", []),
        }
        await indexer.index_folder("INBOX")  # no messages in db — should not raise
