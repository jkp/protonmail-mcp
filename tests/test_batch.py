"""Tests for email_mcp.tools.batch — batch MCP tools."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from email_mcp.imap import ImapError, ImapMutator


@pytest.fixture
def mock_imap():
    from email_mcp.imap import ImapMutator

    m = AsyncMock(spec=ImapMutator)
    m.batch_archive = AsyncMock(return_value=(2, []))
    m.batch_delete = AsyncMock(return_value=(2, []))
    m.batch_move = AsyncMock(return_value=(2, []))
    m.batch_add_flags = AsyncMock(return_value=(2, []))
    return m


@pytest.fixture
def mock_sync_engine():
    from email_mcp.sync import SyncEngine

    m = MagicMock(spec=SyncEngine)
    m.request_reindex = MagicMock()
    return m


@pytest.fixture
def mock_store():
    from email_mcp.store import MaildirStore

    m = MagicMock(spec=MaildirStore)
    m.optimistic_move = MagicMock(return_value=True)
    return m


@pytest.fixture
def mock_resolve_email():
    """Mock _resolve_email to return fake email dicts."""
    from email_mcp.models import Email

    async def _resolve(message_id, folder=None):
        return Email(
            message_id=message_id,
            subject=f"Subject for {message_id}",
            body_plain="Hello",
            folder=folder or "INBOX",
        )

    return _resolve


@pytest.fixture(autouse=True)
def patch_batch(mock_imap, mock_sync_engine, mock_store):
    """Patch module-level dependencies in batch tools."""
    with (
        patch("email_mcp.tools.batch._imap", mock_imap),
        patch("email_mcp.tools.batch._sync_engine", mock_sync_engine),
        patch("email_mcp.tools.batch._store", mock_store),
    ):
        yield


class TestBatchRead:
    async def test_reads_multiple_emails(self, mock_resolve_email):
        from email_mcp.tools.batch import batch_read

        with patch("email_mcp.tools.batch._resolve_email", mock_resolve_email):
            result = await batch_read(
                message_ids=["<msg1@example.com>", "<msg2@example.com>"]
            )
        assert len(result) == 2
        assert result[0]["message_id"] == "<msg1@example.com>"
        assert result[1]["message_id"] == "<msg2@example.com>"

    async def test_includes_error_for_not_found(self):
        from email_mcp.tools.batch import batch_read

        async def _resolve_with_failure(message_id, folder=None):
            if "missing" in message_id:
                return None
            from email_mcp.models import Email

            return Email(message_id=message_id, body_plain="Hello", folder="INBOX")

        with patch("email_mcp.tools.batch._resolve_email", _resolve_with_failure):
            result = await batch_read(
                message_ids=["<msg1@example.com>", "<missing@example.com>"]
            )
        assert len(result) == 2
        assert result[0]["message_id"] == "<msg1@example.com>"
        assert "error" in result[1]
        assert result[1]["message_id"] == "<missing@example.com>"

    async def test_empty_list_returns_empty(self, mock_resolve_email):
        from email_mcp.tools.batch import batch_read

        with patch("email_mcp.tools.batch._resolve_email", mock_resolve_email):
            result = await batch_read(message_ids=[])
        assert result == []


class TestBatchArchive:
    async def test_archives_multiple(self, mock_imap, mock_store, mock_sync_engine):
        from email_mcp.tools.batch import batch_archive

        result = await batch_archive(
            message_ids=["<msg1@example.com>", "<msg2@example.com>"]
        )
        mock_imap.batch_archive.assert_awaited_once_with(
            ["<msg1@example.com>", "<msg2@example.com>"], from_folder=None
        )
        assert result["status"] == "completed"
        assert result["succeeded"] == 2
        assert result["failed"] == 0
        mock_sync_engine.request_reindex.assert_called_once()

    async def test_optimistic_moves_called(self, mock_imap, mock_store):
        from email_mcp.tools.batch import batch_archive

        await batch_archive(
            message_ids=["<msg1@example.com>", "<msg2@example.com>"],
            folder="INBOX",
        )
        assert mock_store.optimistic_move.call_count == 2

    async def test_partial_failure(self, mock_imap, mock_sync_engine):
        from email_mcp.tools.batch import batch_archive

        mock_imap.batch_archive.return_value = (
            1,
            [{"message_id": "<msg2@example.com>", "detail": "Not found"}],
        )
        result = await batch_archive(
            message_ids=["<msg1@example.com>", "<msg2@example.com>"]
        )
        assert result["succeeded"] == 1
        assert result["failed"] == 1
        assert len(result["errors"]) == 1


class TestBatchMarkRead:
    async def test_marks_multiple_read(self, mock_imap, mock_sync_engine):
        from email_mcp.tools.batch import batch_mark_read

        result = await batch_mark_read(
            message_ids=["<msg1@example.com>", "<msg2@example.com>"]
        )
        mock_imap.batch_add_flags.assert_awaited_once_with(
            ["<msg1@example.com>", "<msg2@example.com>"],
            r"\Seen",
            folder=None,
        )
        assert result["status"] == "completed"
        assert result["succeeded"] == 2

    async def test_partial_failure(self, mock_imap):
        from email_mcp.tools.batch import batch_mark_read

        mock_imap.batch_add_flags.return_value = (
            1,
            [{"message_id": "<msg2@example.com>", "detail": "Read-only"}],
        )
        result = await batch_mark_read(
            message_ids=["<msg1@example.com>", "<msg2@example.com>"]
        )
        assert result["succeeded"] == 1
        assert result["failed"] == 1


class TestBatchDelete:
    async def test_requires_confirm(self, mock_imap):
        from email_mcp.tools.batch import batch_delete

        result = await batch_delete(
            message_ids=["<msg1@example.com>"], confirm=False
        )
        assert "error" in result
        mock_imap.batch_delete.assert_not_awaited()

    async def test_deletes_when_confirmed(self, mock_imap, mock_store, mock_sync_engine):
        from email_mcp.tools.batch import batch_delete

        result = await batch_delete(
            message_ids=["<msg1@example.com>", "<msg2@example.com>"],
            confirm=True,
        )
        mock_imap.batch_delete.assert_awaited_once()
        assert result["status"] == "completed"
        assert result["succeeded"] == 2
        mock_sync_engine.request_reindex.assert_called_once()

    async def test_optimistic_moves_to_trash(self, mock_imap, mock_store):
        from email_mcp.tools.batch import batch_delete

        await batch_delete(
            message_ids=["<msg1@example.com>"],
            folder="INBOX",
            confirm=True,
        )
        mock_store.optimistic_move.assert_called_once_with(
            "<msg1@example.com>", "Trash", "INBOX"
        )

    async def test_partial_failure(self, mock_imap, mock_sync_engine):
        from email_mcp.tools.batch import batch_delete

        mock_imap.batch_delete.return_value = (
            1,
            [{"message_id": "<msg2@example.com>", "detail": "Not found"}],
        )
        result = await batch_delete(
            message_ids=["<msg1@example.com>", "<msg2@example.com>"],
            confirm=True,
        )
        assert result["succeeded"] == 1
        assert result["failed"] == 1
        assert len(result["errors"]) == 1


class TestConnectionErrors:
    async def test_batch_archive_connection_error(self, mock_imap, mock_store):
        from email_mcp.tools.batch import batch_archive

        mock_imap.batch_archive.side_effect = ImapError("Connection refused")
        result = await batch_archive(message_ids=["<msg1@example.com>"])
        assert "error" in result
        assert result["error"] == "imap_error"
        mock_store.optimistic_move.assert_not_called()

    async def test_batch_delete_connection_error(self, mock_imap, mock_store):
        from email_mcp.tools.batch import batch_delete

        mock_imap.batch_delete.side_effect = ImapError("Connection refused")
        result = await batch_delete(message_ids=["<msg1@example.com>"], confirm=True)
        assert "error" in result
        assert result["error"] == "imap_error"
        mock_store.optimistic_move.assert_not_called()

    async def test_batch_mark_read_connection_error(self, mock_imap):
        from email_mcp.tools.batch import batch_mark_read

        mock_imap.batch_add_flags.side_effect = ImapError("Connection refused")
        result = await batch_mark_read(message_ids=["<msg1@example.com>"])
        assert "error" in result
        assert result["error"] == "imap_error"
