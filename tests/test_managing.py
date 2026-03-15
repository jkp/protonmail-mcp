"""Tests for email_mcp.tools.managing — IMAP-first managing tools."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_imap():
    from email_mcp.imap import ImapMutator

    m = AsyncMock(spec=ImapMutator)
    return m


@pytest.fixture
def mock_sync_engine():
    from email_mcp.sync import SyncEngine

    m = MagicMock(spec=SyncEngine)
    m.request_reindex = MagicMock()
    m.sync = AsyncMock()
    return m


@pytest.fixture
def mock_store():
    from email_mcp.store import MaildirStore

    m = MagicMock(spec=MaildirStore)
    m.optimistic_move = MagicMock(return_value=True)
    return m


@pytest.fixture
def mock_searcher():
    from email_mcp.search import NotmuchSearcher

    m = AsyncMock(spec=NotmuchSearcher)
    return m


@pytest.fixture(autouse=True)
def patch_managing(mock_imap, mock_sync_engine, mock_store, mock_searcher):
    """Patch module-level dependencies in managing tools."""
    with (
        patch("email_mcp.tools.managing._imap", mock_imap),
        patch("email_mcp.tools.managing._sync_engine", mock_sync_engine),
        patch("email_mcp.tools.managing._store", mock_store),
        patch("email_mcp.tools.managing._searcher", mock_searcher),
    ):
        yield


class TestArchive:
    async def test_imap_first_then_optimistic_move(self, mock_imap, mock_store, mock_sync_engine):
        from email_mcp.tools.managing import archive

        result = await archive("<test@example.com>", folder="INBOX")
        mock_imap.archive.assert_awaited_once_with("<test@example.com>", from_folder="INBOX")
        mock_store.optimistic_move.assert_called_once_with("<test@example.com>", "Archive", "INBOX")
        mock_sync_engine.request_reindex.assert_called_once()
        assert result["status"] == "archived"

    async def test_imap_failure_returns_error(self, mock_imap, mock_store):
        from email_mcp.imap import ImapError
        from email_mcp.tools.managing import archive

        mock_imap.archive.side_effect = ImapError("Connection refused")
        result = await archive("<test@example.com>")
        assert "error" in result
        mock_store.optimistic_move.assert_not_called()

    async def test_optimistic_move_failure_still_succeeds(self, mock_imap, mock_store):
        from email_mcp.tools.managing import archive

        mock_store.optimistic_move.return_value = False
        result = await archive("<test@example.com>")
        assert result["status"] == "archived"


class TestDelete:
    async def test_imap_first_then_optimistic_move(self, mock_imap, mock_store, mock_sync_engine):
        from email_mcp.tools.managing import delete

        result = await delete("<test@example.com>", folder="INBOX")
        mock_imap.delete.assert_awaited_once_with("<test@example.com>", from_folder="INBOX")
        mock_store.optimistic_move.assert_called_once_with("<test@example.com>", "Trash", "INBOX")
        mock_sync_engine.request_reindex.assert_called_once()
        assert result["status"] == "deleted"


class TestMoveEmail:
    async def test_imap_first_then_optimistic_move(self, mock_imap, mock_store, mock_sync_engine):
        from email_mcp.tools.managing import move_email

        result = await move_email("<test@example.com>", "Sent", from_folder="INBOX")
        mock_imap.move.assert_awaited_once_with(
            "<test@example.com>", "Sent", from_folder="INBOX"
        )
        mock_store.optimistic_move.assert_called_once_with("<test@example.com>", "Sent", "INBOX")
        mock_sync_engine.request_reindex.assert_called_once()
        assert result["status"] == "moved"


class TestArchiveThread:
    async def test_archives_all_messages(self, mock_imap, mock_store, mock_sync_engine, mock_searcher):
        from email_mcp.tools.managing import archive_thread

        mock_store.root = "/mail"
        mock_searcher.find_thread_messages.return_value = [
            {"message_id": "msg1@example.com", "path": "/mail/INBOX/cur/msg1"},
            {"message_id": "msg2@example.com", "path": "/mail/INBOX/cur/msg2"},
        ]
        result = await archive_thread("<msg1@example.com>")
        assert mock_imap.archive.await_count == 2
        assert result["archived"] == 2

    async def test_skips_already_archived(self, mock_imap, mock_store, mock_sync_engine, mock_searcher):
        from email_mcp.tools.managing import archive_thread

        mock_store.root = "/mail"
        mock_searcher.find_thread_messages.return_value = [
            {"message_id": "msg1@example.com", "path": "/mail/INBOX/cur/msg1"},
            {"message_id": "msg2@example.com", "path": "/mail/Archive/cur/msg2"},
        ]
        result = await archive_thread("<msg1@example.com>")
        assert mock_imap.archive.await_count == 1
        assert result["archived"] == 1
        assert result["skipped"] == 1

    async def test_thread_not_found(self, mock_searcher):
        from email_mcp.tools.managing import archive_thread

        mock_searcher.find_thread_messages.return_value = []
        result = await archive_thread("<missing@example.com>")
        assert "error" in result

    async def test_partial_failures(self, mock_imap, mock_store, mock_searcher, mock_sync_engine):
        from email_mcp.imap import ImapError
        from email_mcp.tools.managing import archive_thread

        mock_store.root = "/mail"
        mock_searcher.find_thread_messages.return_value = [
            {"message_id": "msg1@example.com", "path": "/mail/INBOX/cur/msg1"},
            {"message_id": "msg2@example.com", "path": "/mail/INBOX/cur/msg2"},
        ]
        # First succeeds, second fails
        mock_imap.archive.side_effect = [None, ImapError("Timeout")]
        result = await archive_thread("<msg1@example.com>")
        assert result["archived"] == 1
        assert result["failed"] == 1


class TestSyncNow:
    async def test_sync_now(self, mock_sync_engine):
        from email_mcp.tools.managing import sync_now

        mock_sync_engine.sync = AsyncMock()
        result = await sync_now()
        mock_sync_engine.sync.assert_awaited_once()
        assert result["status"] == "synced"

    async def test_sync_now_error(self, mock_sync_engine):
        from email_mcp.tools.managing import sync_now

        mock_sync_engine.sync = AsyncMock(side_effect=Exception("Connection refused"))
        result = await sync_now()
        assert result["status"] == "error"
