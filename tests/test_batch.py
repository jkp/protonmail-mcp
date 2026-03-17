"""Tests for email_mcp.tools.batch — batch MCP tools."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from email_mcp.imap import ImapError, ImapMutator
from email_mcp.models import SearchResult


@pytest.fixture
def mock_imap():
    from email_mcp.imap import ImapMutator

    m = AsyncMock(spec=ImapMutator)
    m.batch_archive = AsyncMock(return_value=(2, []))
    m.batch_delete = AsyncMock(return_value=(2, []))
    m.batch_move = AsyncMock(return_value=(2, []))
    m.batch_add_flags = AsyncMock(return_value=(2, []))
    m.batch_move_by_folder = AsyncMock(return_value=(2, []))
    m.batch_add_flags_by_folder = AsyncMock(return_value=(2, []))
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
def mock_searcher():
    """Mock NotmuchSearcher for query-based batch tools."""
    from email_mcp.search import NotmuchSearcher

    m = AsyncMock(spec=NotmuchSearcher)
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
def patch_batch(mock_imap, mock_sync_engine, mock_store, mock_searcher):
    """Patch module-level dependencies in batch tools."""
    with (
        patch("email_mcp.tools.batch._imap", mock_imap),
        patch("email_mcp.tools.batch._sync_engine", mock_sync_engine),
        patch("email_mcp.tools.batch._store", mock_store),
        patch("email_mcp.tools.batch._searcher", mock_searcher),
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
            ["<msg1@example.com>", "<msg2@example.com>"], from_folder="INBOX"
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
            [{"message_id": "<msg2@example.com>", "reason": "Not found"}],
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
            [{"message_id": "<msg2@example.com>", "reason": "Read-only"}],
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
            [{"message_id": "<msg2@example.com>", "reason": "Not found"}],
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


def _make_search_results(count: int, folder: str = "INBOX") -> list[SearchResult]:
    """Helper to create fake search results."""
    return [
        SearchResult(
            message_id=f"<msg{i}@example.com>",
            folders=[folder],
            subject=f"Subject {i}",
            date="2026-03-17",
            authors="sender@example.com",
        )
        for i in range(1, count + 1)
    ]


class TestSearchAndMarkRead:
    async def test_dry_run_returns_count_and_samples(self, mock_searcher):
        from email_mcp.tools.batch import search_and_mark_read

        results = _make_search_results(5)
        mock_searcher.search.return_value = results

        result = await search_and_mark_read(query="from:newsletter", dry_run=True)

        assert result["would_affect"] == 5
        assert "sample_subjects" in result
        assert len(result["sample_subjects"]) == 5

    async def test_dry_run_caps_sample_subjects(self, mock_searcher):
        from email_mcp.tools.batch import search_and_mark_read

        results = _make_search_results(25)
        mock_searcher.search.return_value = results

        result = await search_and_mark_read(query="from:newsletter", dry_run=True)

        assert result["would_affect"] == 25
        assert len(result["sample_subjects"]) <= 10

    async def test_dry_run_includes_by_folder(self, mock_searcher):
        from email_mcp.tools.batch import search_and_mark_read

        results = [
            SearchResult(
                message_id="<a@ex.com>", folders=["INBOX", "Sent"], subject="A",
                date="2026-03-17", authors="x",
            ),
            SearchResult(
                message_id="<b@ex.com>", folders=["INBOX"], subject="B",
                date="2026-03-17", authors="y",
            ),
        ]
        mock_searcher.search.return_value = results

        result = await search_and_mark_read(query="from:x", dry_run=True)

        assert result["by_folder"] == {"INBOX": 2, "Sent": 1}

    async def test_execute_calls_imap_with_folder_hints(
        self, mock_searcher, mock_imap
    ):
        from email_mcp.tools.batch import search_and_mark_read

        results = _make_search_results(3, folder="INBOX")
        mock_searcher.search.return_value = results

        result = await search_and_mark_read(query="from:newsletter", dry_run=False)

        mock_imap.batch_add_flags_by_folder.assert_awaited_once()
        call_args = mock_imap.batch_add_flags_by_folder.call_args
        ids_by_folder = call_args[0][0]
        assert "INBOX" in ids_by_folder
        assert len(ids_by_folder["INBOX"]) == 3
        assert result["succeeded"] == 2  # mock default
        assert result["failed"] == 0

    async def test_execute_groups_by_folder(self, mock_searcher, mock_imap):
        from email_mcp.tools.batch import search_and_mark_read

        results = [
            SearchResult(
                message_id="<a@ex.com>", folders=["INBOX"], subject="A",
                date="2026-03-17", authors="x",
            ),
            SearchResult(
                message_id="<b@ex.com>", folders=["Sent"], subject="B",
                date="2026-03-17", authors="y",
            ),
        ]
        mock_searcher.search.return_value = results

        await search_and_mark_read(query="*", dry_run=False)

        call_args = mock_imap.batch_add_flags_by_folder.call_args
        ids_by_folder = call_args[0][0]
        assert set(ids_by_folder.keys()) == {"INBOX", "Sent"}
        assert ids_by_folder["INBOX"] == ["<a@ex.com>"]
        assert ids_by_folder["Sent"] == ["<b@ex.com>"]

    async def test_multi_folder_message_added_to_all_folders(
        self, mock_searcher, mock_imap
    ):
        """Self-sent emails appear in both INBOX and Sent."""
        from email_mcp.tools.batch import search_and_mark_read

        results = [
            SearchResult(
                message_id="<self@ex.com>", folders=["Sent", "INBOX"],
                subject="Self-sent", date="2026-03-17", authors="x",
            ),
        ]
        mock_searcher.search.return_value = results

        await search_and_mark_read(query="*", dry_run=False)

        call_args = mock_imap.batch_add_flags_by_folder.call_args
        ids_by_folder = call_args[0][0]
        assert "Sent" in ids_by_folder
        assert "INBOX" in ids_by_folder
        assert ids_by_folder["Sent"] == ["<self@ex.com>"]
        assert ids_by_folder["INBOX"] == ["<self@ex.com>"]

    async def test_execute_reports_errors(self, mock_searcher, mock_imap):
        from email_mcp.tools.batch import search_and_mark_read

        results = _make_search_results(3)
        mock_searcher.search.return_value = results
        mock_imap.batch_add_flags_by_folder.return_value = (
            2,
            [{"message_id": "<msg3@example.com>", "reason": "not found in INBOX"}],
        )

        result = await search_and_mark_read(query="from:x", dry_run=False)

        assert result["succeeded"] == 2
        assert result["failed"] == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["message_id"] == "<msg3@example.com>"

    async def test_translates_query(self, mock_searcher):
        from email_mcp.tools.batch import search_and_mark_read

        mock_searcher.search.return_value = []

        await search_and_mark_read(query="from:newsletter", dry_run=True)

        # search should be called with the translated query
        mock_searcher.search.assert_awaited_once()

    async def test_empty_results(self, mock_searcher):
        from email_mcp.tools.batch import search_and_mark_read

        mock_searcher.search.return_value = []

        result = await search_and_mark_read(query="from:nobody", dry_run=True)
        assert result["would_affect"] == 0

    async def test_default_is_dry_run(self, mock_searcher):
        from email_mcp.tools.batch import search_and_mark_read

        mock_searcher.search.return_value = _make_search_results(2)

        result = await search_and_mark_read(query="from:x")

        assert "would_affect" in result

    async def test_search_failure_returns_error(self, mock_searcher):
        from email_mcp.tools.batch import search_and_mark_read

        mock_searcher.search.side_effect = Exception("notmuch DB locked")

        result = await search_and_mark_read(query="from:x", dry_run=True)

        assert result["error"] == "search_failed"
        assert "locked" in result["detail"]

    async def test_truncates_and_reports_remaining(self, mock_searcher, mock_imap):
        from email_mcp.tools.batch import _MAX_BATCH_SIZE, search_and_mark_read

        mock_searcher.search.return_value = _make_search_results(_MAX_BATCH_SIZE + 10)

        result = await search_and_mark_read(query="from:x", dry_run=False)

        # Should process _MAX_BATCH_SIZE, report remaining
        assert result["remaining"] == 10
        mock_imap.batch_add_flags_by_folder.assert_awaited_once()

    async def test_dry_run_shows_full_count_over_limit(self, mock_searcher):
        from email_mcp.tools.batch import _MAX_BATCH_SIZE, search_and_mark_read

        mock_searcher.search.return_value = _make_search_results(_MAX_BATCH_SIZE + 10)

        result = await search_and_mark_read(query="from:x", dry_run=True)

        assert result["would_affect"] == _MAX_BATCH_SIZE + 10


class TestSearchAndArchive:
    async def test_dry_run_returns_count(self, mock_searcher):
        from email_mcp.tools.batch import search_and_archive

        mock_searcher.search.return_value = _make_search_results(10)

        result = await search_and_archive(query="from:newsletter", dry_run=True)

        assert result["would_affect"] == 10
        assert "sample_subjects" in result

    async def test_execute_calls_batch_move_by_folder(
        self, mock_searcher, mock_imap, mock_store, mock_sync_engine
    ):
        from email_mcp.tools.batch import search_and_archive

        results = _make_search_results(3, folder="INBOX")
        mock_searcher.search.return_value = results

        result = await search_and_archive(query="from:newsletter", dry_run=False)

        mock_imap.batch_move_by_folder.assert_awaited_once()
        call_args = mock_imap.batch_move_by_folder.call_args
        ids_by_folder = call_args[0][0]
        to_folder = call_args[0][1]
        assert to_folder == "Archive"
        assert "INBOX" in ids_by_folder
        assert result["succeeded"] == 2  # mock default
        mock_sync_engine.request_reindex.assert_called_once()

    async def test_execute_optimistic_moves(
        self, mock_searcher, mock_imap, mock_store
    ):
        from email_mcp.tools.batch import search_and_archive

        results = _make_search_results(2, folder="INBOX")
        mock_searcher.search.return_value = results

        await search_and_archive(query="from:x", dry_run=False)

        assert mock_store.optimistic_move.call_count == 2

    async def test_skips_optimistic_move_for_failed(
        self, mock_searcher, mock_imap, mock_store
    ):
        from email_mcp.tools.batch import search_and_archive

        results = _make_search_results(2, folder="INBOX")
        mock_searcher.search.return_value = results
        mock_imap.batch_move_by_folder.return_value = (
            1,
            [{"message_id": "<msg2@example.com>", "reason": "not found in INBOX"}],
        )

        await search_and_archive(query="from:x", dry_run=False)

        # Only 1 optimistic move (msg1 succeeded, msg2 failed)
        assert mock_store.optimistic_move.call_count == 1

    async def test_default_is_dry_run(self, mock_searcher):
        from email_mcp.tools.batch import search_and_archive

        mock_searcher.search.return_value = _make_search_results(2)

        result = await search_and_archive(query="from:x")
        assert "would_affect" in result


class TestSearchAndDelete:
    async def test_dry_run_returns_count(self, mock_searcher):
        from email_mcp.tools.batch import search_and_delete

        mock_searcher.search.return_value = _make_search_results(5)

        result = await search_and_delete(query="from:spam", dry_run=True)

        assert result["would_affect"] == 5

    async def test_execute_moves_to_trash(
        self, mock_searcher, mock_imap, mock_sync_engine
    ):
        from email_mcp.tools.batch import search_and_delete

        results = _make_search_results(3, folder="INBOX")
        mock_searcher.search.return_value = results

        result = await search_and_delete(query="from:spam", dry_run=False)

        mock_imap.batch_move_by_folder.assert_awaited_once()
        call_args = mock_imap.batch_move_by_folder.call_args
        to_folder = call_args[0][1]
        assert to_folder == "Trash"
        mock_sync_engine.request_reindex.assert_called_once()

    async def test_execute_optimistic_moves_to_trash(
        self, mock_searcher, mock_imap, mock_store
    ):
        from email_mcp.tools.batch import search_and_delete

        results = _make_search_results(2, folder="INBOX")
        mock_searcher.search.return_value = results

        await search_and_delete(query="from:spam", dry_run=False)

        calls = mock_store.optimistic_move.call_args_list
        assert all(c[0][1] == "Trash" for c in calls)

    async def test_default_is_dry_run(self, mock_searcher):
        from email_mcp.tools.batch import search_and_delete

        mock_searcher.search.return_value = _make_search_results(2)

        result = await search_and_delete(query="from:x")
        assert "would_affect" in result
