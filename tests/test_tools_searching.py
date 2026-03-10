"""Tests for searching tools."""

from unittest.mock import AsyncMock, patch

from protonmail_mcp.models import SearchResult
from protonmail_mcp.tools.searching import search


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
