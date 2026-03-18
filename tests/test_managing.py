"""Tests for email_mcp.tools.managing — ProtonMail API mutation tools (v4)."""

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from email_mcp.db import Database, MessageRow


def _make_row(pm_id: str, message_id: str, folder: str = "INBOX") -> MessageRow:
    return MessageRow(
        pm_id=pm_id,
        message_id=message_id,
        subject=f"Test {pm_id}",
        sender_name="Alice",
        sender_email="alice@example.com",
        recipients=[],
        date=int(time.time()),
        unread=True,
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
    d.messages.upsert(_make_row("pm-archived", "<archived@example.com>", "Archive"))
    return d


@pytest.fixture
def mock_api():
    api = MagicMock()
    api.label_messages = AsyncMock()
    api.mark_read = AsyncMock()
    api.mark_unread = AsyncMock()
    return api


@pytest.fixture(autouse=True)
def patch_managing(db, mock_api):
    with (
        patch("email_mcp.tools.managing.db", db),
        patch("email_mcp.tools.managing._api", mock_api),
    ):
        yield


class TestArchive:
    async def test_archives_by_message_id(self, mock_api, db):
        from email_mcp.tools.managing import archive

        result = await archive("<msg1@example.com>")
        mock_api.label_messages.assert_awaited_once_with(["pm-001"], "6")
        assert result["status"] == "archived"

    async def test_archive_updates_sqlite(self, db):
        from email_mcp.tools.managing import archive

        await archive("<msg1@example.com>")
        row = db.messages.get("pm-001")
        assert row.folder == "Archive"

    async def test_archive_not_found(self, mock_api):
        from email_mcp.tools.managing import archive

        result = await archive("<missing@example.com>")
        assert "error" in result
        mock_api.label_messages.assert_not_awaited()

    async def test_archive_api_error_returns_error(self, mock_api):
        from email_mcp.proton_api import ProtonAPIError
        from email_mcp.tools.managing import archive

        mock_api.label_messages.side_effect = ProtonAPIError(422, "Invalid")
        result = await archive("<msg1@example.com>")
        assert "error" in result


class TestDelete:
    async def test_deletes_by_message_id(self, mock_api):
        from email_mcp.tools.managing import delete

        result = await delete("<msg1@example.com>")
        mock_api.label_messages.assert_awaited_once_with(["pm-001"], "3")
        assert result["status"] == "deleted"

    async def test_delete_updates_sqlite(self, db):
        from email_mcp.tools.managing import delete

        await delete("<msg1@example.com>")
        row = db.messages.get("pm-001")
        assert row.folder == "Trash"

    async def test_delete_not_found(self, mock_api):
        from email_mcp.tools.managing import delete

        result = await delete("<missing@example.com>")
        assert "error" in result
        mock_api.label_messages.assert_not_awaited()


class TestMoveEmail:
    async def test_move_to_archive(self, mock_api):
        from email_mcp.tools.managing import move_email

        result = await move_email("<msg1@example.com>", "Archive")
        mock_api.label_messages.assert_awaited_once_with(["pm-001"], "6")
        assert result["status"] == "moved"

    async def test_move_to_trash(self, mock_api):
        from email_mcp.tools.managing import move_email

        await move_email("<msg1@example.com>", "Trash")
        mock_api.label_messages.assert_awaited_once_with(["pm-001"], "3")

    async def test_move_to_spam(self, mock_api):
        from email_mcp.tools.managing import move_email

        await move_email("<msg1@example.com>", "Spam")
        mock_api.label_messages.assert_awaited_once_with(["pm-001"], "4")

    async def test_move_to_inbox(self, mock_api):
        from email_mcp.tools.managing import move_email

        await move_email("<archived@example.com>", "INBOX")
        mock_api.label_messages.assert_awaited_once_with(["pm-archived"], "0")

    async def test_move_updates_sqlite(self, db):
        from email_mcp.tools.managing import move_email

        await move_email("<msg1@example.com>", "Archive")
        row = db.messages.get("pm-001")
        assert row.folder == "Archive"

    async def test_move_unknown_folder_returns_error(self, mock_api):
        from email_mcp.tools.managing import move_email

        result = await move_email("<msg1@example.com>", "UnknownFolder")
        assert "error" in result
        mock_api.label_messages.assert_not_awaited()

    async def test_move_not_found(self, mock_api):
        from email_mcp.tools.managing import move_email

        result = await move_email("<missing@example.com>", "Archive")
        assert "error" in result
        mock_api.label_messages.assert_not_awaited()


class TestMarkRead:
    async def test_marks_read(self, mock_api):
        from email_mcp.tools.managing import mark_read

        result = await mark_read("<msg1@example.com>")
        mock_api.mark_read.assert_awaited_once_with(["pm-001"])
        assert result["status"] == "ok"

    async def test_marks_read_updates_sqlite(self, db):
        from email_mcp.tools.managing import mark_read

        await mark_read("<msg1@example.com>")
        row = db.messages.get("pm-001")
        assert row.unread is False

    async def test_mark_read_not_found(self, mock_api):
        from email_mcp.tools.managing import mark_read

        result = await mark_read("<missing@example.com>")
        assert "error" in result
        mock_api.mark_read.assert_not_awaited()


class TestArchiveThread:
    async def test_archives_single_message(self, mock_api):
        from email_mcp.tools.managing import archive_thread

        result = await archive_thread("<msg1@example.com>")
        mock_api.label_messages.assert_awaited_once_with(["pm-001"], "6")
        assert result["archived"] >= 1

    async def test_archive_thread_not_found(self, mock_api):
        from email_mcp.tools.managing import archive_thread

        result = await archive_thread("<missing@example.com>")
        assert "error" in result

    async def test_skips_already_archived(self, mock_api):
        from email_mcp.tools.managing import archive_thread

        result = await archive_thread("<archived@example.com>")
        # Already in Archive — skipped, not re-archived
        mock_api.label_messages.assert_not_awaited()
        assert result["skipped"] == 1


class TestSyncNow:
    async def test_sync_now_returns_db_stats(self, db):
        from email_mcp.tools.managing import sync_now

        result = await sync_now()
        assert result["status"] == "ok"
        assert "message_count" in result
