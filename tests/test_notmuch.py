"""Tests for notmuch search and UID extraction."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from protonmail_mcp.notmuch import (
    NotmuchError,
    NotmuchSearcher,
    _first_matching_message,
    extract_folder,
    extract_uid,
)


class TestExtractUid:
    def test_standard_mbsync_filename(self) -> None:
        path = "/home/user/mail/INBOX/cur/1709020800.M123456P1234.hostname,S=5678,U=42:2,S"
        assert extract_uid(path) == "42"

    def test_large_uid(self) -> None:
        path = "/home/user/mail/INBOX/cur/1709020800.M123456P1234.hostname,S=5678,U=99999:2,S"
        assert extract_uid(path) == "99999"

    def test_no_uid_returns_none(self) -> None:
        path = "/home/user/mail/INBOX/cur/1709020800.M123456P1234.hostname:2,S"
        assert extract_uid(path) is None

    def test_uid_at_end_of_filename(self) -> None:
        path = "/home/user/mail/INBOX/cur/1709020800.hostname,U=7"
        assert extract_uid(path) == "7"


class TestExtractFolder:
    def test_inbox(self) -> None:
        path = "/home/user/mail/INBOX/cur/file.msg"
        assert extract_folder(path, "/home/user/mail") == "INBOX"

    def test_nested_folder(self) -> None:
        path = "/home/user/mail/Work/Projects/cur/file.msg"
        assert extract_folder(path, "/home/user/mail") == "Work/Projects"

    def test_sent(self) -> None:
        path = "/home/user/mail/Sent/cur/file.msg"
        assert extract_folder(path, "/home/user/mail") == "Sent"

    def test_trailing_slash_on_root(self) -> None:
        path = "/home/user/mail/INBOX/cur/file.msg"
        assert extract_folder(path, "/home/user/mail/") == "INBOX"

    def test_new_subdir(self) -> None:
        path = "/home/user/mail/INBOX/new/file.msg"
        assert extract_folder(path, "/home/user/mail") == "INBOX"


class TestFirstMatchingMessage:
    def test_finds_match_in_simple_thread(self) -> None:
        thread = [[{"match": True, "headers": {"Subject": "Test"}}, []]]
        msg = _first_matching_message(thread)
        assert msg is not None
        assert msg["headers"]["Subject"] == "Test"

    def test_skips_non_matching(self) -> None:
        thread = [
            [{"match": False, "headers": {"Subject": "No"}}, []],
            [{"match": True, "headers": {"Subject": "Yes"}}, []],
        ]
        msg = _first_matching_message(thread)
        assert msg is not None
        assert msg["headers"]["Subject"] == "Yes"

    def test_returns_none_for_empty(self) -> None:
        assert _first_matching_message([]) is None

    def test_finds_match_in_nested_replies(self) -> None:
        thread = [[
            {"match": False, "headers": {"Subject": "Parent"}},
            [[{"match": True, "headers": {"Subject": "Reply"}}, []]],
        ]]
        msg = _first_matching_message(thread)
        assert msg is not None
        assert msg["headers"]["Subject"] == "Reply"


def _mock_process(stdout: str = "", stderr: str = "", returncode: int = 0) -> AsyncMock:
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    proc.returncode = returncode
    proc.kill = MagicMock()
    return proc


class TestNotmuchSearcher:
    @pytest.fixture
    def searcher(self) -> NotmuchSearcher:
        return NotmuchSearcher(
            bin_path="notmuch",
            maildir_root="/home/user/mail",
            timeout=10,
        )

    async def test_search_returns_results_with_metadata(
        self, searcher: NotmuchSearcher, sample_notmuch_show_json: str
    ) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout=sample_notmuch_show_json)):
            results = await searcher.search("tag:inbox")
            assert len(results) == 2
            assert results[0].uid == "42"
            assert results[0].folder == "INBOX"
            assert results[0].subject == "Test Subject"
            assert results[0].authors == "Alice <alice@example.com>"
            assert results[0].date == "Mon, 10 Mar 2026 08:00:00 +0000"
            assert results[1].uid == "100"
            assert results[1].folder == "Sent"
            assert results[1].subject == "Another Subject"

    async def test_search_skips_files_without_uid(self, searcher: NotmuchSearcher) -> None:
        show_json = """[[[{
            "match": true, "filename": ["/home/user/mail/INBOX/cur/no_uid:2,S"],
            "headers": {"Subject": "No UID"}, "crypto": {}, "tags": []
        }, []]]]"""
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout=show_json)):
            results = await searcher.search("tag:inbox")
            assert results == []

    async def test_search_empty_results(self, searcher: NotmuchSearcher) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout="[]")):
            results = await searcher.search("tag:nonexistent")
            assert results == []

    async def test_search_with_limit(self, searcher: NotmuchSearcher, sample_notmuch_show_json: str) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout=sample_notmuch_show_json)):
            results = await searcher.search("*", limit=1)
            assert len(results) == 1
            assert results[0].uid == "42"

    async def test_search_with_offset(self, searcher: NotmuchSearcher, sample_notmuch_show_json: str) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout=sample_notmuch_show_json)):
            results = await searcher.search("*", limit=1, offset=1)
            assert len(results) == 1
            assert results[0].uid == "100"

    async def test_search_passes_query_to_notmuch(self, searcher: NotmuchSearcher) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout="[]")) as mock_exec:
            await searcher.search("from:alice@example.com AND tag:inbox")
            args = mock_exec.call_args[0]
            assert "show" in args
            assert "--body=false" in args
            assert "from:alice@example.com AND tag:inbox" in args

    async def test_search_timeout(self, searcher: NotmuchSearcher) -> None:
        proc = _mock_process()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(NotmuchError, match="timed out"):
                await searcher.search("*")
            proc.kill.assert_called_once()

    async def test_search_nonzero_exit(self, searcher: NotmuchSearcher) -> None:
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_process(stderr="notmuch error", returncode=1),
        ):
            with pytest.raises(NotmuchError, match="notmuch error"):
                await searcher.search("*")

    async def test_search_threads(self, searcher: NotmuchSearcher, sample_notmuch_search_json: str) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout=sample_notmuch_search_json)):
            results = await searcher.search_threads("tag:inbox")
            assert len(results) == 2
            assert results[0]["authors"] == "Alice"
            assert results[1]["subject"] == "Another Subject"
