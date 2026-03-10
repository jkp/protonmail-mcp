"""Integration tests using FastMCP in-memory Client."""

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import Client

from protonmail_mcp.models import SearchResult
from protonmail_mcp.server import mcp


def _parse_result(result: Any) -> Any:
    """Extract data from a CallToolResult, handling both dict and list returns."""
    # For dict returns, result.data works directly
    # For list returns, we need to parse from text content
    if result.data and not isinstance(result.data, list):
        return result.data
    text = result.content[0].text
    return json.loads(text)


@pytest.fixture
async def client():
    async with Client(mcp) as c:
        yield c


class TestServerToolRegistration:
    async def test_all_tools_registered(self, client: Client) -> None:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        expected = {
            "list_emails",
            "list_folders",
            "read_email",
            "download_attachment",
            "search",
            "send",
            "reply",
            "forward",
            "archive",
            "delete",
            "move_email",
            "set_identity",
        }
        assert expected == names

    async def test_tool_count(self, client: Client) -> None:
        tools = await client.list_tools()
        assert len(tools) == 12


class TestListEmailsIntegration:
    async def test_list_emails_via_client(self, client: Client) -> None:
        envelope_data = [
            {
                "id": "42",
                "from": {"name": "Alice", "addr": "alice@example.com"},
                "to": [{"name": "Bob", "addr": "bob@example.com"}],
                "subject": "Test Subject",
                "date": "2026-03-10T08:00:00Z",
            }
        ]
        with patch("protonmail_mcp.tools.listing.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=envelope_data)
            result = await client.call_tool("list_emails", {"folder": "INBOX"})
            data = _parse_result(result)
            assert len(data) == 1
            assert data[0]["id"] == "42"
            assert data[0]["subject"] == "Test Subject"


class TestListFoldersIntegration:
    async def test_list_folders_via_client(self, client: Client) -> None:
        folder_data = [
            {"name": "INBOX", "desc": ""},
            {"name": "Sent", "desc": ""},
        ]
        with patch("protonmail_mcp.tools.listing.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=folder_data)
            result = await client.call_tool("list_folders", {})
            data = _parse_result(result)
            assert len(data) == 2
            assert data[0]["name"] == "INBOX"


class TestReadEmailIntegration:
    async def test_read_email_via_client(self, client: Client) -> None:
        template = (
            "From: Alice <alice@example.com>\n"
            "To: bob@example.com\n"
            "Subject: Test Subject\n"
            "Date: 2026-03-10T08:00:00Z\n"
            "\n"
            "<#part type=text/html>\n"
            "<p>Hello <b>world</b></p>\n"
            "<#/part>\n"
        )
        with patch("protonmail_mcp.tools.reading.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=template)
            result = await client.call_tool(
                "read_email", {"email_id": "42", "folder": "INBOX"}
            )
            data = _parse_result(result)
            assert data["id"] == "42"
            assert "**world**" in data["body"]


class TestSearchIntegration:
    async def test_search_via_client(self, client: Client) -> None:
        mock_results = [
            SearchResult(uid="42", folder="INBOX", subject="Test", date="2026-03-10", authors="Alice"),
        ]
        with patch("protonmail_mcp.tools.searching.notmuch") as mock_notmuch:
            mock_notmuch.search = AsyncMock(return_value=mock_results)
            result = await client.call_tool("search", {"query": "from:alice"})
            data = _parse_result(result)
            assert len(data) == 1
            assert data[0]["uid"] == "42"


class TestSendIntegration:
    async def test_send_via_client(self, client: Client) -> None:
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run = AsyncMock(return_value="Message sent")
            result = await client.call_tool(
                "send",
                {"to": "alice@example.com", "subject": "Hi", "body": "Hello!"},
            )
            data = _parse_result(result)
            assert data["status"] == "sent"


class TestArchiveIntegration:
    async def test_archive_via_client(self, client: Client) -> None:
        with patch("protonmail_mcp.tools.managing.himalaya") as mock_himalaya:
            mock_himalaya.run = AsyncMock(return_value="done")
            result = await client.call_tool(
                "archive", {"email_id": "42", "folder": "INBOX"}
            )
            data = _parse_result(result)
            assert data["status"] == "archived"


class TestDeleteIntegration:
    async def test_delete_via_client(self, client: Client) -> None:
        with patch("protonmail_mcp.tools.managing.himalaya") as mock_himalaya:
            mock_himalaya.run = AsyncMock(return_value="done")
            result = await client.call_tool(
                "delete", {"email_id": "42", "folder": "INBOX"}
            )
            data = _parse_result(result)
            assert data["status"] == "deleted"


class TestMoveEmailIntegration:
    async def test_move_via_client(self, client: Client) -> None:
        with patch("protonmail_mcp.tools.managing.himalaya") as mock_himalaya:
            mock_himalaya.run = AsyncMock(return_value="done")
            result = await client.call_tool(
                "move_email",
                {"email_id": "42", "from_folder": "INBOX", "to_folder": "Work"},
            )
            data = _parse_result(result)
            assert data["status"] == "moved"


class TestSetIdentityIntegration:
    async def test_set_identity_via_client(self, client: Client) -> None:
        with patch("protonmail_mcp.tools.managing.himalaya"):
            result = await client.call_tool(
                "set_identity", {"account": "work"}
            )
            data = _parse_result(result)
            assert data["status"] == "identity_set"
            assert data["account"] == "work"
