"""Live read-only tests against local Maildir + notmuch."""

import pytest
from fastmcp import Client

from tests.live.conftest import _parse_result, live, skip_no_maildir

pytestmark = [live, skip_no_maildir, pytest.mark.timeout(120)]


class TestListFolders:
    async def test_standard_folders_exist(self, live_client: Client) -> None:
        result = await live_client.call_tool("list_folders", {})
        folders = _parse_result(result)
        names = {f["name"] for f in folders}
        for expected in ("INBOX", "Sent", "Trash"):
            assert expected in names, f"Missing folder: {expected}"

    async def test_folders_have_counts(self, live_client: Client) -> None:
        result = await live_client.call_tool("list_folders", {})
        folders = _parse_result(result)
        for folder in folders:
            assert "count" in folder
            assert "unread" in folder


class TestListEmails:
    async def test_returns_list_structure(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "list_emails", {"folder": "INBOX", "limit": 5}
        )
        emails = _parse_result(result)
        assert isinstance(emails, list)

    async def test_email_has_required_fields(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "list_emails", {"folder": "INBOX", "limit": 50}
        )
        emails = _parse_result(result)
        if not emails:
            pytest.skip("No emails in INBOX to validate")
        for email in emails:
            assert "message_id" in email
            assert "from" in email
            assert "subject" in email
            assert "date" in email
            assert "folder" in email

    async def test_list_50_emails(self, live_client: Client) -> None:
        """Validate that 50 real emails parse without crash."""
        result = await live_client.call_tool(
            "list_emails", {"folder": "INBOX", "limit": 50}
        )
        emails = _parse_result(result)
        assert isinstance(emails, list)


class TestReadEmail:
    async def test_reads_first_email(self, live_client: Client) -> None:
        list_result = await live_client.call_tool(
            "list_emails", {"folder": "INBOX", "limit": 5}
        )
        emails = _parse_result(list_result)
        if not emails:
            pytest.skip("No emails in INBOX")

        result = await live_client.call_tool(
            "read_email", {"message_id": emails[0]["message_id"], "folder": "INBOX"}
        )
        data = _parse_result(result)
        assert data["subject"]
        assert data["body"]
        assert data["from"]
        assert "date" in data

    async def test_no_raw_html_in_body(self, live_client: Client) -> None:
        """HTML emails should be converted to markdown."""
        result = await live_client.call_tool(
            "list_emails", {"folder": "INBOX", "limit": 20}
        )
        emails = _parse_result(result)
        if not emails:
            pytest.skip("No emails in INBOX")

        for email in emails[:5]:
            read_result = await live_client.call_tool(
                "read_email", {"message_id": email["message_id"], "folder": "INBOX"}
            )
            data = _parse_result(read_result)
            body = data["body"]
            assert "<html>" not in body.lower()
            assert "<body>" not in body.lower()
            if body.strip():
                return

        pytest.skip("No emails with readable body found")

    async def test_read_multiple_emails(self, live_client: Client) -> None:
        """Read 10 emails and verify they all parse correctly."""
        result = await live_client.call_tool(
            "list_emails", {"folder": "INBOX", "limit": 10}
        )
        emails = _parse_result(result)
        if not emails:
            pytest.skip("No emails in INBOX")

        failures = []
        for email in emails:
            try:
                read_result = await live_client.call_tool(
                    "read_email", {"message_id": email["message_id"], "folder": "INBOX"}
                )
                data = _parse_result(read_result)
                if "error" in data:
                    failures.append(f"{email['message_id']}: {data['error']}")
            except Exception as e:
                failures.append(f"{email['message_id']}: {e}")

        assert not failures, "Failed to read:\n" + "\n".join(failures)
