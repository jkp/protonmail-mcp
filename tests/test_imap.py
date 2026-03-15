"""Tests for email_mcp.imap — IMAP mutator (IMAPClient-based)."""

from unittest.mock import MagicMock, patch

import pytest

from email_mcp.imap import ImapError, ImapMutator


@pytest.fixture
def mutator():
    return ImapMutator(
        host="127.0.0.1",
        port=1143,
        username="user",
        password="pass",
        starttls=True,
        cert_path="",
    )


def _mock_imapclient():
    """Create a mock IMAPClient."""
    client = MagicMock()
    client.login = MagicMock()
    client.starttls = MagicMock()
    client.logout = MagicMock()
    client.select_folder = MagicMock()
    client.search = MagicMock(return_value=[42])
    client.copy = MagicMock()
    client.delete_messages = MagicMock()
    client.expunge = MagicMock()
    client.set_flags = MagicMock()
    client.add_flags = MagicMock()
    client.remove_flags = MagicMock()
    client.list_folders = MagicMock(return_value=[
        ([], b"/", "INBOX"),
        ([], b"/", "Archive"),
        ([], b"/", "Sent"),
        ([], b"/", "Trash"),
    ])
    return client


class TestConnect:
    async def test_connect_with_starttls(self, mutator):
        mock_client = _mock_imapclient()
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client) as mock_cls:
            await mutator.connect()
        mock_cls.assert_called_once_with("127.0.0.1", port=1143, ssl=False)
        mock_client.starttls.assert_called_once()
        mock_client.login.assert_called_once_with("user", "pass")

    async def test_connect_without_starttls(self):
        m = ImapMutator(
            host="127.0.0.1", port=993, username="user", password="pass",
            starttls=False, cert_path="",
        )
        mock_client = _mock_imapclient()
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await m.connect()
        mock_client.starttls.assert_not_called()

    async def test_disconnect(self, mutator):
        mock_client = _mock_imapclient()
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            await mutator.disconnect()
        mock_client.logout.assert_called_once()


class TestFindUid:
    async def test_find_uid_in_specified_folder(self, mutator):
        mock_client = _mock_imapclient()
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            folder, uid = await mutator._find_uid("<test@example.com>", folder="INBOX")
        assert folder == "INBOX"
        assert uid == 42
        mock_client.select_folder.assert_called_with("INBOX", readonly=True)

    async def test_find_uid_searches_inbox_first(self, mutator):
        mock_client = _mock_imapclient()
        # INBOX returns nothing, Archive finds it
        mock_client.search = MagicMock(side_effect=[[], [99]])
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            folder, uid = await mutator._find_uid("<test@example.com>")
        assert folder == "Archive"
        assert uid == 99

    async def test_find_uid_not_found_raises(self, mutator):
        mock_client = _mock_imapclient()
        mock_client.search = MagicMock(return_value=[])
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            with pytest.raises(ImapError, match="not found"):
                await mutator._find_uid("<missing@example.com>")


class TestMove:
    async def test_move_calls_copy_delete_expunge(self, mutator):
        mock_client = _mock_imapclient()
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            await mutator.move("<test@example.com>", "Archive", from_folder="INBOX")

        mock_client.copy.assert_called_once_with([42], "Archive")
        mock_client.delete_messages.assert_called_once_with([42])
        mock_client.expunge.assert_called_once_with([42])

    async def test_move_imap_failure_raises(self, mutator):
        mock_client = _mock_imapclient()
        mock_client.copy = MagicMock(side_effect=Exception("Permission denied"))
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            with pytest.raises(ImapError):
                await mutator.move("<test@example.com>", "Archive", from_folder="INBOX")


class TestDelete:
    async def test_delete_moves_to_trash(self, mutator):
        mock_client = _mock_imapclient()
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            await mutator.delete("<test@example.com>", from_folder="INBOX")
        mock_client.copy.assert_called_once_with([42], "Trash")


class TestArchive:
    async def test_archive_moves_to_archive(self, mutator):
        mock_client = _mock_imapclient()
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            await mutator.archive("<test@example.com>", from_folder="INBOX")
        mock_client.copy.assert_called_once_with([42], "Archive")


class TestFlags:
    async def test_set_flags(self, mutator):
        mock_client = _mock_imapclient()
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            await mutator.set_flags("<test@example.com>", r"\Seen \Flagged", folder="INBOX")
        mock_client.set_flags.assert_called_once_with([42], [r"\Seen", r"\Flagged"])

    async def test_add_flags(self, mutator):
        mock_client = _mock_imapclient()
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            await mutator.add_flags("<test@example.com>", r"\Seen", folder="INBOX")
        mock_client.add_flags.assert_called_once_with([42], [r"\Seen"])

    async def test_remove_flags(self, mutator):
        mock_client = _mock_imapclient()
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            await mutator.remove_flags("<test@example.com>", r"\Seen", folder="INBOX")
        mock_client.remove_flags.assert_called_once_with([42], [r"\Seen"])


class TestAutoReconnect:
    async def test_ensure_connected_reconnects_when_disconnected(self, mutator):
        mock_client = _mock_imapclient()
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            assert mutator._client is not None
            mutator._client = None
            await mutator._ensure_connected()
            assert mutator._client is not None

    async def test_ensure_connected_noop_when_connected(self, mutator):
        mock_client = _mock_imapclient()
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            original = mutator._client
            await mutator._ensure_connected()
            assert mutator._client is original
