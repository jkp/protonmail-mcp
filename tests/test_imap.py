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


class TestBatchFindUidsSync:
    async def test_finds_uids_in_specified_folder(self, mutator):
        mock_client = _mock_imapclient()
        # All messages found in INBOX
        mock_client.search = MagicMock(side_effect=[[42], [99]])
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            result = await mutator._batch_find_uids(
                ["<msg1@example.com>", "<msg2@example.com>"], folder="INBOX"
            )
        assert result == {"INBOX": [("<msg1@example.com>", 42), ("<msg2@example.com>", 99)]}

    async def test_groups_by_folder_when_no_folder_hint(self, mutator):
        mock_client = _mock_imapclient()
        # Folder-outer loop: INBOX selected once, search both messages
        # msg1 found in INBOX, msg2 not. Then Archive selected, msg2 found.
        mock_client.search = MagicMock(side_effect=[[42], [], [99]])
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            result = await mutator._batch_find_uids(
                ["<msg1@example.com>", "<msg2@example.com>"]
            )
        assert "INBOX" in result
        assert result["INBOX"] == [("<msg1@example.com>", 42)]
        assert "Archive" in result
        assert result["Archive"] == [("<msg2@example.com>", 99)]

    async def test_not_found_messages_returned_in_errors(self, mutator):
        mock_client = _mock_imapclient()
        # msg1 found in INBOX, missing not found in INBOX (only folder searched)
        mock_client.search = MagicMock(side_effect=[[42], []])
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            result, errors = await mutator._batch_find_uids_with_errors(
                ["<msg1@example.com>", "<missing@example.com>"], folder="INBOX"
            )
        assert "INBOX" in result
        assert len(errors) == 1
        assert errors[0]["message_id"] == "<missing@example.com>"


class TestBatchMoveSync:
    async def test_batch_move_single_folder(self, mutator):
        mock_client = _mock_imapclient()
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            succeeded, errors = await mutator.batch_move(
                ["<msg1@example.com>", "<msg2@example.com>"],
                "Archive",
                from_folder="INBOX",
            )
        assert succeeded == 2
        assert errors == []
        # Should batch COPY/DELETE/EXPUNGE with all UIDs
        mock_client.copy.assert_called_once()
        mock_client.delete_messages.assert_called_once()
        mock_client.expunge.assert_called_once()

    async def test_batch_move_error_isolation(self, mutator):
        mock_client = _mock_imapclient()
        mock_client.copy = MagicMock(side_effect=Exception("Folder not found"))
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            succeeded, errors = await mutator.batch_move(
                ["<msg1@example.com>"], "NonExistent", from_folder="INBOX"
            )
        assert succeeded == 0
        assert len(errors) == 1

    async def test_batch_move_message_not_found(self, mutator):
        mock_client = _mock_imapclient()
        mock_client.search = MagicMock(return_value=[])
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            succeeded, errors = await mutator.batch_move(
                ["<missing@example.com>"], "Archive", from_folder="INBOX"
            )
        assert succeeded == 0
        assert len(errors) == 1
        assert "not found" in errors[0]["reason"].lower()


class TestBatchAddFlagsSync:
    async def test_batch_add_flags_single_folder(self, mutator):
        mock_client = _mock_imapclient()
        mock_client.search = MagicMock(side_effect=[[42], [99]])
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            succeeded, errors = await mutator.batch_add_flags(
                ["<msg1@example.com>", "<msg2@example.com>"],
                r"\Seen",
                folder="INBOX",
            )
        assert succeeded == 2
        assert errors == []
        # Single STORE call with both UIDs
        mock_client.add_flags.assert_called_once()
        call_args = mock_client.add_flags.call_args
        assert set(call_args[0][0]) == {42, 99}

    async def test_batch_add_flags_error_isolation(self, mutator):
        mock_client = _mock_imapclient()
        mock_client.add_flags = MagicMock(side_effect=Exception("Read-only"))
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            succeeded, errors = await mutator.batch_add_flags(
                ["<msg1@example.com>"], r"\Seen", folder="INBOX"
            )
        assert succeeded == 0
        assert len(errors) == 1


class TestBatchArchive:
    async def test_batch_archive_delegates_to_batch_move(self, mutator):
        mock_client = _mock_imapclient()
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            succeeded, errors = await mutator.batch_archive(
                ["<msg1@example.com>"], from_folder="INBOX"
            )
        assert succeeded == 1
        mock_client.copy.assert_called_once()
        # Verify destination is Archive
        assert mock_client.copy.call_args[0][1] == "Archive"


class TestBatchDelete:
    async def test_batch_delete_delegates_to_batch_move(self, mutator):
        mock_client = _mock_imapclient()
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            succeeded, errors = await mutator.batch_delete(
                ["<msg1@example.com>"], from_folder="INBOX"
            )
        assert succeeded == 1
        mock_client.copy.assert_called_once()
        # Verify destination is Trash
        assert mock_client.copy.call_args[0][1] == "Trash"


class TestBatchMoveByFolder:
    async def test_moves_with_preresolved_folders(self, mutator):
        mock_client = _mock_imapclient()
        mock_client.search = MagicMock(side_effect=[[42], [99]])
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            succeeded, errors = await mutator.batch_move_by_folder(
                {"INBOX": ["<msg1@example.com>", "<msg2@example.com>"]},
                "Archive",
            )
        assert succeeded == 2
        assert errors == []
        mock_client.copy.assert_called_once()
        assert mock_client.copy.call_args[0][1] == "Archive"

    async def test_handles_multiple_folders(self, mutator):
        mock_client = _mock_imapclient()
        # INBOX: find msg1 (uid 42), Sent: find msg2 (uid 99)
        mock_client.search = MagicMock(side_effect=[[42], [99]])
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            succeeded, errors = await mutator.batch_move_by_folder(
                {
                    "INBOX": ["<msg1@example.com>"],
                    "Sent": ["<msg2@example.com>"],
                },
                "Archive",
            )
        assert succeeded == 2
        assert errors == []
        assert mock_client.copy.call_count == 2

    async def test_not_found_returns_error_with_reason(self, mutator):
        mock_client = _mock_imapclient()
        mock_client.search = MagicMock(side_effect=[[42], []])
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            succeeded, errors = await mutator.batch_move_by_folder(
                {"INBOX": ["<msg1@example.com>", "<missing@example.com>"]},
                "Archive",
            )
        assert succeeded == 1
        assert len(errors) == 1
        assert errors[0]["message_id"] == "<missing@example.com>"
        assert "INBOX" in errors[0]["reason"]


class TestBatchAddFlagsByFolder:
    async def test_adds_flags_with_preresolved_folders(self, mutator):
        mock_client = _mock_imapclient()
        mock_client.search = MagicMock(side_effect=[[42], [99]])
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            succeeded, errors = await mutator.batch_add_flags_by_folder(
                {"INBOX": ["<msg1@example.com>", "<msg2@example.com>"]},
                [r"\Seen"],
            )
        assert succeeded == 2
        assert errors == []
        mock_client.add_flags.assert_called_once()

    async def test_not_found_returns_error_with_reason(self, mutator):
        mock_client = _mock_imapclient()
        mock_client.search = MagicMock(side_effect=[[42], []])
        with patch("email_mcp.imap.IMAPClient", return_value=mock_client):
            await mutator.connect()
            succeeded, errors = await mutator.batch_add_flags_by_folder(
                {"INBOX": ["<msg1@example.com>", "<missing@example.com>"]},
                [r"\Seen"],
            )
        assert succeeded == 1
        assert len(errors) == 1
        assert errors[0]["message_id"] == "<missing@example.com>"
        assert "INBOX" in errors[0]["reason"]


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
