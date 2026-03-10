"""Tests for searching tools."""

from unittest.mock import AsyncMock, patch

from protonmail_mcp.models import SearchResult
from protonmail_mcp.tools.searching import _build_himalaya_query, _resolve_uid, _translate_query, search


class TestTranslateQuery:
    def test_has_attachment(self) -> None:
        assert _translate_query("has:attachment") == "tag:attachment"

    def test_is_unread(self) -> None:
        assert _translate_query("is:unread") == "tag:unread"

    def test_is_read(self) -> None:
        assert _translate_query("is:read") == "not tag:unread"

    def test_is_starred(self) -> None:
        assert _translate_query("is:starred") == "tag:flagged"

    def test_in_folder(self) -> None:
        assert _translate_query("in:inbox") == "folder:inbox"
        assert _translate_query("in:sent") == "folder:sent"

    def test_label(self) -> None:
        assert _translate_query("label:important") == "tag:important"

    def test_filename(self) -> None:
        assert _translate_query("filename:report.pdf") == "attachment:report.pdf"

    def test_newer_than(self) -> None:
        assert _translate_query("newer_than:7d") == "date:7days.."

    def test_older_than(self) -> None:
        assert _translate_query("older_than:30d") == "date:..30days"

    def test_combined_query(self) -> None:
        assert _translate_query("from:alice has:attachment is:unread") == "from:alice tag:attachment tag:unread"

    def test_native_notmuch_passthrough(self) -> None:
        assert _translate_query("from:alice AND tag:inbox") == "from:alice AND tag:inbox"

    def test_no_partial_word_match(self) -> None:
        assert _translate_query("subject:has:colon") == "subject:has:colon"


class TestBuildHimalayaQuery:
    def test_subject_and_from(self) -> None:
        assert _build_himalaya_query("Test Subject", "Alice <alice@example.com>") == "subject Test Subject and from alice@example.com"

    def test_subject_only(self) -> None:
        assert _build_himalaya_query("Test Subject", "") == "subject Test Subject"

    def test_from_only(self) -> None:
        assert _build_himalaya_query("", "alice@example.com") == "from alice@example.com"

    def test_empty_both(self) -> None:
        assert _build_himalaya_query("", "") == ""

    def test_long_subject_truncated(self) -> None:
        result = _build_himalaya_query("one two three four five six seven", "")
        assert result == "subject one two three four five"

    def test_subject_strips_dash_words(self) -> None:
        result = _build_himalaya_query("-negative safe word", "")
        assert result == "subject safe word"

    def test_from_extracts_email_from_angle_brackets(self) -> None:
        result = _build_himalaya_query("", "Bob Smith <bob@example.com>")
        assert "from bob@example.com" in result


class TestResolveUid:
    async def test_returns_uid_on_match(self) -> None:
        with patch("protonmail_mcp.tools.searching.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=[{"id": "999"}])
            uid = await _resolve_uid("INBOX", "Test Subject", "Alice <alice@example.com>")
            assert uid == "999"

    async def test_returns_none_on_empty_results(self) -> None:
        with patch("protonmail_mcp.tools.searching.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=[])
            uid = await _resolve_uid("INBOX", "Test", "Alice")
            assert uid is None

    async def test_returns_none_on_exception(self) -> None:
        with patch("protonmail_mcp.tools.searching.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(side_effect=Exception("connection failed"))
            uid = await _resolve_uid("INBOX", "Test", "Alice")
            assert uid is None

    async def test_returns_none_when_no_query(self) -> None:
        uid = await _resolve_uid("INBOX", "", "")
        assert uid is None


class TestSearch:
    async def test_returns_search_results_with_resolved_uids(self) -> None:
        mock_results = [
            SearchResult(uid="42", folder="INBOX", subject="Test", date="2026-03-10", authors="Alice <alice@example.com>"),
            SearchResult(uid="43", folder="INBOX", subject="Other", date="2026-03-10", authors="Bob <bob@example.com>"),
        ]
        with (
            patch("protonmail_mcp.tools.searching.notmuch") as mock_notmuch,
            patch("protonmail_mcp.tools.searching.himalaya") as mock_himalaya,
        ):
            mock_notmuch.search = AsyncMock(return_value=mock_results)
            mock_himalaya.run_json = AsyncMock(side_effect=[
                [{"id": "100"}],  # resolved UID for first result
                [{"id": "200"}],  # resolved UID for second result
            ])
            result = await search(query="from:alice")
            assert len(result) == 2
            assert result[0]["uid"] == "100"
            assert result[1]["uid"] == "200"
            assert result[0]["folder"] == "INBOX"

    async def test_falls_back_to_notmuch_uid_on_resolve_failure(self) -> None:
        mock_results = [
            SearchResult(uid="42", folder="INBOX", subject="Test", date="2026-03-10", authors="Alice"),
        ]
        with (
            patch("protonmail_mcp.tools.searching.notmuch") as mock_notmuch,
            patch("protonmail_mcp.tools.searching.himalaya") as mock_himalaya,
        ):
            mock_notmuch.search = AsyncMock(return_value=mock_results)
            mock_himalaya.run_json = AsyncMock(side_effect=Exception("connection failed"))
            result = await search(query="from:alice")
            assert result[0]["uid"] == "42"

    async def test_passes_limit_and_offset(self) -> None:
        with patch("protonmail_mcp.tools.searching.notmuch") as mock_notmuch:
            mock_notmuch.search = AsyncMock(return_value=[])
            await search(query="tag:inbox", limit=10, offset=5)
            mock_notmuch.search.assert_called_once_with("tag:inbox", limit=10, offset=5)

    async def test_default_limit(self) -> None:
        with patch("protonmail_mcp.tools.searching.notmuch") as mock_notmuch:
            mock_notmuch.search = AsyncMock(return_value=[])
            await search(query="*")
            mock_notmuch.search.assert_called_once_with("*", limit=20, offset=0)

    async def test_empty_results(self) -> None:
        with patch("protonmail_mcp.tools.searching.notmuch") as mock_notmuch:
            mock_notmuch.search = AsyncMock(return_value=[])
            result = await search(query="nonexistent")
            assert result == []
