"""Tests for managing tools (archive, delete, move_email, set_identity)."""

from unittest.mock import AsyncMock, patch

from protonmail_mcp.tools.managing import archive, delete, move_email, set_identity


class TestArchive:
    async def test_moves_to_archive(self) -> None:
        with patch("protonmail_mcp.tools.managing.himalaya") as mock_himalaya:
            mock_himalaya.run = AsyncMock(return_value="Message moved")
            result = await archive(email_id="42", folder="INBOX")
            args = mock_himalaya.run.call_args[0]
            assert "message" in args
            assert "move" in args
            assert "42" in args
            assert "--folder" in args
            assert "INBOX" in args
            assert "Archive" in args
            assert result["status"] == "archived"


class TestDelete:
    async def test_deletes_message(self) -> None:
        with patch("protonmail_mcp.tools.managing.himalaya") as mock_himalaya:
            mock_himalaya.run = AsyncMock(return_value="Message deleted")
            result = await delete(email_id="42", folder="INBOX")
            args = mock_himalaya.run.call_args[0]
            assert "message" in args
            assert "delete" in args
            assert "42" in args
            assert "--folder" in args
            assert "INBOX" in args
            assert result["status"] == "deleted"


class TestMoveEmail:
    async def test_moves_message(self) -> None:
        with patch("protonmail_mcp.tools.managing.himalaya") as mock_himalaya:
            mock_himalaya.run = AsyncMock(return_value="Message moved")
            result = await move_email(email_id="42", from_folder="INBOX", to_folder="Work")
            args = mock_himalaya.run.call_args[0]
            assert "message" in args
            assert "move" in args
            assert "42" in args
            assert "--folder" in args
            assert "INBOX" in args
            assert "Work" in args
            assert result["status"] == "moved"


class TestSetIdentity:
    async def test_sets_default_account(self) -> None:
        with patch("protonmail_mcp.tools.managing.himalaya") as mock_himalaya:
            result = await set_identity(account="work")
            assert mock_himalaya.account == "work"
            assert result["status"] == "identity_set"
            assert result["account"] == "work"
