"""Tests for the background body indexer."""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from email_mcp.body_indexer import BodyIndexer
from email_mcp.db import Database, MessageRow


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


def _insert_message(db: Database, pm_id: str, body_indexed: bool = False, folder: str = "INBOX") -> None:
    db.messages.upsert(MessageRow(
        pm_id=pm_id,
        message_id=f"{pm_id}@example.com",
        subject="Test",
        sender_name="Alice",
        sender_email="alice@example.com",
        recipients=[],
        date=int(time.time()),
        unread=True,
        label_ids=["0"],
        folder=folder,
        size=1024,
        has_attachments=False,
        body_indexed=body_indexed,
    ))


@pytest.fixture
def mock_decryptor() -> MagicMock:
    dec = MagicMock()
    dec.fetch_and_decrypt = AsyncMock(return_value=("Hello, this is the email body.", []))
    dec.fetch_and_decrypt_batch = AsyncMock(return_value={})
    return dec


@pytest.fixture
def indexer(db: Database, mock_decryptor: MagicMock) -> BodyIndexer:
    bi = BodyIndexer(db=db, decryptor=mock_decryptor, workers=2)
    bi.retry_delays = [0, 0, 0]  # no sleep in tests
    return bi


class TestSingleFetch:
    async def test_indexes_body_for_queued_message(
        self, indexer: BodyIndexer, db: Database
    ) -> None:
        _insert_message(db, "pm-001")
        await indexer._fetch_and_index("pm-001")
        body = db.bodies.get("pm-001")
        assert body == "Hello, this is the email body."

    async def test_marks_body_indexed(
        self, indexer: BodyIndexer, db: Database
    ) -> None:
        _insert_message(db, "pm-001")
        await indexer._fetch_and_index("pm-001")
        assert db.messages.get("pm-001").body_indexed is True

    async def test_skips_already_indexed(
        self, indexer: BodyIndexer, db: Database, mock_decryptor: MagicMock
    ) -> None:
        _insert_message(db, "pm-001", body_indexed=True)
        await indexer._fetch_and_index("pm-001")
        mock_decryptor.fetch_and_decrypt.assert_not_called()

    async def test_handles_decrypt_error(
        self, indexer: BodyIndexer, db: Database, mock_decryptor: MagicMock
    ) -> None:
        _insert_message(db, "pm-001")
        mock_decryptor.fetch_and_decrypt.side_effect = Exception("decrypt failed")
        await indexer._fetch_and_index("pm-001")  # should not raise
        assert db.messages.get("pm-001").body_indexed is False

    async def test_unknown_pm_id_is_noop(
        self, indexer: BodyIndexer, db: Database, mock_decryptor: MagicMock
    ) -> None:
        await indexer._fetch_and_index("does-not-exist")
        mock_decryptor.fetch_and_decrypt.assert_not_called()

    async def test_indexes_attachments(
        self, indexer: BodyIndexer, db: Database, mock_decryptor: MagicMock
    ) -> None:
        _insert_message(db, "pm-001")
        mock_decryptor.fetch_and_decrypt.return_value = (
            "body text",
            [{"att_id": "a1", "filename": "doc.pdf", "size": 1024, "mime_type": "application/pdf"}],
        )
        await indexer._fetch_and_index("pm-001")
        atts = db.attachments.list_for_message("pm-001")
        assert len(atts) == 1
        assert atts[0]["filename"] == "doc.pdf"


class TestQueueProcessing:
    async def test_processes_queued_items(
        self, indexer: BodyIndexer, db: Database
    ) -> None:
        _insert_message(db, "pm-001")
        _insert_message(db, "pm-002")

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
        await asyncio.wait_for(indexer.run_queue(queue), timeout=1.0)


class TestBulkIndex:
    async def test_indexes_all_unindexed(
        self, indexer: BodyIndexer, db: Database, mock_decryptor: MagicMock
    ) -> None:
        _insert_message(db, "pm-001")
        _insert_message(db, "pm-002")
        mock_decryptor.fetch_and_decrypt_batch.return_value = {
            "pm-001": ("Body 1", []),
            "pm-002": ("Body 2", []),
        }

        await indexer.index_unindexed()

        assert db.bodies.get("pm-001") == "Body 1"
        assert db.bodies.get("pm-002") == "Body 2"
        assert db.messages.get("pm-001").body_indexed is True
        assert db.messages.get("pm-002").body_indexed is True

    async def test_filters_by_folder(
        self, indexer: BodyIndexer, db: Database, mock_decryptor: MagicMock
    ) -> None:
        _insert_message(db, "pm-001", folder="INBOX")
        _insert_message(db, "pm-002", folder="Archive")
        mock_decryptor.fetch_and_decrypt_batch.return_value = {
            "pm-001": ("Body 1", []),
        }

        await indexer.index_unindexed(folder="INBOX")

        # Only pm-001 should be passed to batch
        call_args = mock_decryptor.fetch_and_decrypt_batch.call_args
        assert "pm-001" in call_args[0][0]
        assert "pm-002" not in call_args[0][0]

    async def test_skips_already_indexed(
        self, indexer: BodyIndexer, db: Database, mock_decryptor: MagicMock
    ) -> None:
        _insert_message(db, "pm-001", body_indexed=True)

        await indexer.index_unindexed()

        mock_decryptor.fetch_and_decrypt_batch.assert_not_called()

    async def test_empty_is_noop(
        self, indexer: BodyIndexer, db: Database, mock_decryptor: MagicMock
    ) -> None:
        await indexer.index_unindexed()
        mock_decryptor.fetch_and_decrypt_batch.assert_not_called()
