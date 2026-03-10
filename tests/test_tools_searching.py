"""Tests for searching tools."""

from unittest.mock import AsyncMock, patch

from protonmail_mcp.models import SearchResult
from protonmail_mcp.tools.searching import _translate_query, search


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


class TestSearch:
    async def test_returns_search_results(self) -> None:
        mock_results = [
            SearchResult(uid="42", folder="INBOX", subject="Test", date="2026-03-10", authors="Alice"),
            SearchResult(uid="43", folder="INBOX", subject="Other", date="2026-03-10", authors="Bob"),
        ]
        with patch("protonmail_mcp.tools.searching.notmuch") as mock_notmuch:
            mock_notmuch.search = AsyncMock(return_value=mock_results)
            result = await search(query="from:alice")
            assert len(result) == 2
            assert result[0]["uid"] == "42"
            assert result[0]["folder"] == "INBOX"

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
