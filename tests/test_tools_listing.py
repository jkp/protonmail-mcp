"""Tests for listing tools (list_emails, list_folders)."""

import json
from unittest.mock import AsyncMock, patch

from protonmail_mcp.tools.listing import list_emails, list_folders


class TestListEmails:
    async def test_returns_formatted_envelopes(self, sample_envelope_json: str) -> None:
        data = json.loads(sample_envelope_json)
        with patch("protonmail_mcp.tools.listing.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=data)
            result = await list_emails()
            assert len(result) == 2
            assert result[0]["id"] == "42"
            assert result[0]["subject"] == "Test Subject"
            assert result[0]["from"] == "Alice <alice@example.com>"

    async def test_passes_folder_parameter(self, sample_envelope_json: str) -> None:
        data = json.loads(sample_envelope_json)
        with patch("protonmail_mcp.tools.listing.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=data)
            await list_emails(folder="Sent")
            mock_himalaya.run_json.assert_called_once()
            args = mock_himalaya.run_json.call_args[0]
            assert "--folder" in args
            assert "Sent" in args

    async def test_passes_page_parameters(self, sample_envelope_json: str) -> None:
        data = json.loads(sample_envelope_json)
        with patch("protonmail_mcp.tools.listing.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=data)
            await list_emails(page=2, page_size=25)
            args = mock_himalaya.run_json.call_args[0]
            assert "--page" in args
            assert "2" in args
            assert "--page-size" in args
            assert "25" in args

    async def test_default_folder_is_inbox(self, sample_envelope_json: str) -> None:
        data = json.loads(sample_envelope_json)
        with patch("protonmail_mcp.tools.listing.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=data)
            await list_emails()
            args = mock_himalaya.run_json.call_args[0]
            assert "INBOX" in args


class TestListFolders:
    async def test_returns_folder_names(self, sample_folder_json: str) -> None:
        data = json.loads(sample_folder_json)
        with patch("protonmail_mcp.tools.listing.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=data)
            result = await list_folders()
            assert len(result) == 5
            assert result[0]["name"] == "INBOX"
            assert result[4]["name"] == "Trash"
