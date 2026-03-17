"""Tests for email_mcp.tools.batch — v4 ProtonMail API batch tools."""

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from email_mcp.db import Database, MessageRow


def _make_row(pm_id: str, message_id: str, folder: str = "INBOX", unread: bool = True) -> MessageRow:
    return MessageRow(
        pm_id=pm_id,
        message_id=message_id,
        subject=f"Subject {pm_id}",
        sender_name="Alice",
        sender_email="alice@example.com",
        recipients=[{"name": "Bob", "address": "bob@example.com"}],
        date=int(time.time()),
        unread=unread,
        label_ids=["0"],
        folder=folder,
        size=512,
        has_attachments=False,
        body_indexed=False,
    )


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    d.messages.upsert(_make_row("pm-001", "<msg1@example.com>", "INBOX"))
    d.messages.upsert(_make_row("pm-002", "<msg2@example.com>", "INBOX"))
    d.messages.upsert(_make_row("pm-003", "<msg3@example.com>", "Sent"))
    d.messages.upsert(_make_row("pm-trash", "<trash@example.com>", "Trash"))
    return d


@pytest.fixture
def mock_api():
    api = MagicMock()
    api.label_messages = AsyncMock()
    api.mark_read = AsyncMock()
    return api


@pytest.fixture(autouse=True)
def patch_batch(db, mock_api):
    with (
        patch("email_mcp.tools.batch.db", db),
        patch("email_mcp.tools.batch._api", mock_api),
        patch("email_mcp.tools.managing.db", db),
    ):
        yield


class TestBatchRead:
    async def test_reads_multiple_emails(self, db):
        from email_mcp.tools.batch import batch_read

        result = await batch_read(message_ids=["<msg1@example.com>", "<msg2@example.com>"])
        assert len(result) == 2
        assert result[0]["message_id"] == "<msg1@example.com>"
        assert result[1]["message_id"] == "<msg2@example.com>"

    async def test_includes_error_for_not_found(self, db):
        from email_mcp.tools.batch import batch_read

        result = await batch_read(message_ids=["<msg1@example.com>", "<missing@example.com>"])
        assert len(result) == 2
        assert result[0]["message_id"] == "<msg1@example.com>"
        assert "error" in result[1]
        assert result[1]["message_id"] == "<missing@example.com>"

    async def test_empty_list_returns_empty(self):
        from email_mcp.tools.batch import batch_read

        result = await batch_read(message_ids=[])
        assert result == []


class TestBatchArchive:
    async def test_archives_by_pm_id_lookup(self, mock_api):
        from email_mcp.tools.batch import batch_archive

        result = await batch_archive(message_ids=["<msg1@example.com>", "<msg2@example.com>"])
        mock_api.label_messages.assert_awaited_once_with(["pm-001", "pm-002"], "6")
        assert result["status"] == "completed"
        assert result["succeeded"] == 2

    async def test_updates_sqlite_optimistically(self, db):
        from email_mcp.tools.batch import batch_archive

        await batch_archive(message_ids=["<msg1@example.com>"])
        assert db.messages.get("pm-001").folder == "Archive"

    async def test_not_found_reported_in_errors(self, mock_api):
        from email_mcp.tools.batch import batch_archive

        result = await batch_archive(message_ids=["<msg1@example.com>", "<missing@example.com>"])
        assert result["succeeded"] == 1
        assert result["failed"] == 1
        assert any(e.get("message_id") == "<missing@example.com>" for e in result["errors"])


class TestBatchMarkRead:
    async def test_marks_read_via_api(self, mock_api):
        from email_mcp.tools.batch import batch_mark_read

        result = await batch_mark_read(message_ids=["<msg1@example.com>", "<msg2@example.com>"])
        mock_api.mark_read.assert_awaited_once_with(["pm-001", "pm-002"])
        assert result["status"] == "completed"
        assert result["succeeded"] == 2

    async def test_updates_sqlite_unread_flag(self, db):
        from email_mcp.tools.batch import batch_mark_read

        await batch_mark_read(message_ids=["<msg1@example.com>"])
        assert db.messages.get("pm-001").unread is False


class TestBatchDelete:
    async def test_requires_confirm(self):
        from email_mcp.tools.batch import batch_delete

        result = await batch_delete(message_ids=["<msg1@example.com>"], confirm=False)
        assert "error" in result

    async def test_deletes_when_confirmed(self, mock_api):
        from email_mcp.tools.batch import batch_delete

        result = await batch_delete(message_ids=["<msg1@example.com>"], confirm=True)
        mock_api.label_messages.assert_awaited_once_with(["pm-001"], "3")
        assert result["status"] == "completed"
        assert result["succeeded"] == 1

    async def test_updates_sqlite_to_trash(self, db):
        from email_mcp.tools.batch import batch_delete

        await batch_delete(message_ids=["<msg1@example.com>"], confirm=True)
        assert db.messages.get("pm-001").folder == "Trash"


class TestSearchAndMarkRead:
    async def test_dry_run_returns_count_and_samples(self, db):
        from email_mcp.tools.batch import search_and_mark_read

        result = await search_and_mark_read(query="is:unread", dry_run=True)
        assert "would_affect" in result
        assert result["would_affect"] >= 2
        assert "sample_subjects" in result

    async def test_dry_run_does_not_call_api(self, mock_api):
        from email_mcp.tools.batch import search_and_mark_read

        await search_and_mark_read(query="is:unread", dry_run=True)
        mock_api.mark_read.assert_not_awaited()

    async def test_execute_calls_api(self, mock_api):
        from email_mcp.tools.batch import search_and_mark_read

        result = await search_and_mark_read(query="is:unread", dry_run=False)
        mock_api.mark_read.assert_awaited()
        assert "succeeded" in result

    async def test_execute_updates_sqlite(self, db):
        from email_mcp.tools.batch import search_and_mark_read

        await search_and_mark_read(query="in:inbox is:unread", dry_run=False)
        assert db.messages.get("pm-001").unread is False
        assert db.messages.get("pm-002").unread is False

    async def test_default_is_dry_run(self, mock_api):
        from email_mcp.tools.batch import search_and_mark_read

        result = await search_and_mark_read(query="is:unread")
        assert "would_affect" in result
        mock_api.mark_read.assert_not_awaited()

    async def test_empty_results(self, mock_api):
        from email_mcp.tools.batch import search_and_mark_read

        result = await search_and_mark_read(query="from:nobody@no.where", dry_run=False)
        assert result["succeeded"] == 0
        mock_api.mark_read.assert_not_awaited()


class TestSearchAndArchive:
    async def test_dry_run_returns_count(self, db):
        from email_mcp.tools.batch import search_and_archive

        result = await search_and_archive(query="in:inbox", dry_run=True)
        assert result["would_affect"] == 2

    async def test_execute_calls_api_with_label_6(self, mock_api):
        from email_mcp.tools.batch import search_and_archive

        await search_and_archive(query="in:inbox", dry_run=False)
        call_args = mock_api.label_messages.call_args
        assert call_args[0][1] == "6"

    async def test_execute_updates_sqlite(self, db):
        from email_mcp.tools.batch import search_and_archive

        await search_and_archive(query="in:inbox", dry_run=False)
        assert db.messages.get("pm-001").folder == "Archive"
        assert db.messages.get("pm-002").folder == "Archive"

    async def test_default_is_dry_run(self, mock_api):
        from email_mcp.tools.batch import search_and_archive

        result = await search_and_archive(query="in:inbox")
        assert "would_affect" in result
        mock_api.label_messages.assert_not_awaited()


class TestSearchAndDelete:
    async def test_dry_run_returns_count(self, db):
        from email_mcp.tools.batch import search_and_delete

        # inbox has 2 messages (Trash excluded)
        result = await search_and_delete(query="in:inbox", dry_run=True)
        assert result["would_affect"] == 2

    async def test_skips_messages_already_in_trash(self, db):
        from email_mcp.tools.batch import search_and_delete

        # Query that would match Trash too — Trash messages should be excluded
        result = await search_and_delete(query="*", dry_run=True)
        subjects = result.get("sample_subjects", [])
        # Total should not include the pm-trash message
        assert result["would_affect"] == 3  # INBOX x2 + Sent x1

    async def test_execute_calls_api_with_label_3(self, mock_api):
        from email_mcp.tools.batch import search_and_delete

        await search_and_delete(query="in:inbox", dry_run=False)
        call_args = mock_api.label_messages.call_args
        assert call_args[0][1] == "3"

    async def test_default_is_dry_run(self, mock_api):
        from email_mcp.tools.batch import search_and_delete

        result = await search_and_delete(query="in:inbox")
        assert "would_affect" in result
        mock_api.label_messages.assert_not_awaited()
