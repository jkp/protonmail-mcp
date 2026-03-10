"""Live read-only tests against Protonmail Bridge."""

import pytest
from fastmcp import Client

from tests.live.conftest import _parse_result, live, skip_no_bridge

pytestmark = [live, skip_no_bridge, pytest.mark.timeout(120)]


class TestListFolders:
    async def test_standard_folders_exist(self, live_client: Client) -> None:
        result = await live_client.call_tool("list_folders", {})
        folders = _parse_result(result)
        names = {f["name"] for f in folders}
        for expected in ("INBOX", "Sent", "Trash"):
            assert expected in names, f"Missing folder: {expected}"


class TestListEmails:
    async def test_returns_list_structure(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "list_emails", {"folder": "INBOX", "page_size": 5}
        )
        emails = _parse_result(result)
        assert isinstance(emails, list)

    async def test_envelope_has_required_fields(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "list_emails", {"folder": "INBOX", "page_size": 50}
        )
        emails = _parse_result(result)
        if not emails:
            pytest.skip("No emails in INBOX to validate")
        for email in emails:
            assert "id" in email
            assert "from" in email
            assert "subject" in email
            assert "date" in email
            assert "has_attachment" in email

    async def test_pydantic_validates_50_envelopes(self, live_client: Client) -> None:
        """Validate that 50 real envelopes parse without crash."""
        result = await live_client.call_tool(
            "list_emails", {"folder": "INBOX", "page_size": 50}
        )
        emails = _parse_result(result)
        assert isinstance(emails, list)


class TestReadEmail:
    async def test_reads_first_email(self, live_client: Client) -> None:
        list_result = await live_client.call_tool(
            "list_emails", {"folder": "INBOX", "page_size": 5}
        )
        emails = _parse_result(list_result)
        if not emails:
            pytest.skip("No emails in INBOX")

        result = await live_client.call_tool(
            "read_email", {"email_id": emails[0]["id"], "folder": "INBOX"}
        )
        data = _parse_result(result)
        assert data["subject"]
        assert data["body"]
        assert data["from"]
        assert "date" in data  # date may be empty for some emails

    async def test_body_has_no_part_markers(self, live_client: Client) -> None:
        """Regression: template parser must strip <#part> markers."""
        list_result = await live_client.call_tool(
            "list_emails", {"folder": "INBOX", "page_size": 5}
        )
        emails = _parse_result(list_result)
        if not emails:
            pytest.skip("No emails in INBOX")

        result = await live_client.call_tool(
            "read_email", {"email_id": emails[0]["id"], "folder": "INBOX"}
        )
        data = _parse_result(result)
        assert "<#part" not in data["body"]
        assert "<#/part>" not in data["body"]

    async def test_html_email_converts_to_markdown(self, live_client: Client) -> None:
        """Read existing emails, verify no raw HTML remains."""
        result = await live_client.call_tool(
            "list_emails", {"folder": "INBOX", "page_size": 20}
        )
        emails = _parse_result(result)
        if not emails:
            pytest.skip("No emails in INBOX to test HTML conversion")

        for email in emails[:5]:
            read_result = await live_client.call_tool(
                "read_email", {"email_id": email["id"], "folder": "INBOX"}
            )
            data = _parse_result(read_result)
            body = data["body"]
            assert "<html>" not in body.lower()
            assert "<body>" not in body.lower()
            if body.strip():
                return

        pytest.skip("No emails with readable body found")
