"""Live search tests: notmuch search, Message-ID resolution, attachments."""

import pytest
from fastmcp import Client

from tests.live.conftest import _parse_result, live, skip_no_maildir, skip_no_notmuch

pytestmark = [live, skip_no_maildir, skip_no_notmuch, pytest.mark.timeout(120)]


class TestSearch:
    async def test_search_returns_list(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "search", {"query": "tag:inbox", "limit": 5}
        )
        data = _parse_result(result)
        assert isinstance(data, list)

    async def test_search_result_has_required_fields(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "search", {"query": "tag:inbox", "limit": 5}
        )
        data = _parse_result(result)
        if not data:
            pytest.skip("No search results to validate")
        for item in data:
            assert "message_id" in item
            assert "folders" in item
            assert "subject" in item
            assert "date" in item
            assert "authors" in item


class TestSearchReadBack:
    """Verify search results have valid Message-IDs by reading each email back."""

    @pytest.fixture
    async def inbox_search_results(self, live_client: Client) -> list:
        result = await live_client.call_tool(
            "search", {"query": "tag:inbox", "limit": 10}
        )
        data = _parse_result(result)
        if len(data) < 5:
            pytest.skip("Need at least 5 search results for comprehensive test")
        return data

    async def test_all_search_message_ids_are_readable(
        self, live_client: Client, inbox_search_results: list
    ) -> None:
        """Every search result Message-ID must be readable via read_email."""
        failures = []
        for item in inbox_search_results:
            mid = item["message_id"]
            folder = item["folders"][0] if item.get("folders") else ""
            try:
                result = await live_client.call_tool(
                    "read_email", {"message_id": mid, "folder": folder}
                )
                data = _parse_result(result)
                if "error" in data:
                    failures.append(f"mid={mid} folder={folder}: {data['error']}")
                else:
                    assert data["subject"], f"mid={mid} has empty subject"
            except Exception as e:
                failures.append(f"mid={mid} folder={folder} subject='{item['subject']}': {e}")

        assert not failures, "Failed to read emails:\n" + "\n".join(failures)

    async def test_search_subjects_match_read_subjects(
        self, live_client: Client, inbox_search_results: list
    ) -> None:
        """Subject from search must match subject from read_email."""
        mismatches = []
        for item in inbox_search_results:
            mid = item["message_id"]
            folder = item["folders"][0] if item.get("folders") else ""
            try:
                result = await live_client.call_tool(
                    "read_email", {"message_id": mid, "folder": folder}
                )
                data = _parse_result(result)
                if "error" not in data and data["subject"] != item["subject"]:
                    mismatches.append(
                        f"mid={mid}: search='{item['subject']}' vs read='{data['subject']}'"
                    )
            except Exception as e:
                mismatches.append(f"mid={mid}: read failed: {e}")

        assert not mismatches, "Subject mismatches:\n" + "\n".join(mismatches)


class TestSearchAcrossFolders:
    """Verify search works for emails in different folders."""

    async def test_archive_emails_are_readable(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "search", {"query": "folder:Archive", "limit": 5}
        )
        data = _parse_result(result)
        if not data:
            pytest.skip("No emails in Archive")

        failures = []
        for item in data:
            mid = item["message_id"]
            folder = item["folders"][0] if item.get("folders") else ""
            try:
                result = await live_client.call_tool(
                    "read_email", {"message_id": mid, "folder": folder}
                )
                read_data = _parse_result(result)
                if "error" in read_data:
                    failures.append(f"mid={mid}: {read_data['error']}")
            except Exception as e:
                failures.append(f"mid={mid} subject='{item['subject']}': {e}")

        assert not failures, "Archive read failures:\n" + "\n".join(failures)

    async def test_sent_emails_are_readable(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "search", {"query": "folder:Sent", "limit": 5}
        )
        data = _parse_result(result)
        if not data:
            pytest.skip("No emails in Sent")

        failures = []
        for item in data:
            mid = item["message_id"]
            folder = item["folders"][0] if item.get("folders") else ""
            try:
                result = await live_client.call_tool(
                    "read_email", {"message_id": mid, "folder": folder}
                )
                read_data = _parse_result(result)
                if "error" in read_data:
                    failures.append(f"mid={mid}: {read_data['error']}")
            except Exception as e:
                failures.append(f"mid={mid} subject='{item['subject']}': {e}")

        assert not failures, "Sent read failures:\n" + "\n".join(failures)


class TestSearchAttachmentPipeline:
    """End-to-end: search for attachments -> list -> download."""

    async def test_attachment_emails_have_listable_attachments(
        self, live_client: Client
    ) -> None:
        result = await live_client.call_tool(
            "search", {"query": "tag:attachment", "limit": 5}
        )
        data = _parse_result(result)
        if not data:
            pytest.skip("No emails with attachment tag")

        tested = 0
        failures = []
        for item in data:
            mid = item["message_id"]
            folder = item["folders"][0] if item.get("folders") else ""
            try:
                att_result = await live_client.call_tool(
                    "list_attachments", {"message_id": mid, "folder": folder}
                )
                att_data = _parse_result(att_result)
                if att_data:
                    tested += 1
                    for att in att_data:
                        assert "filename" in att
                        assert "size" in att
                        assert att["size"] > 0
            except Exception as e:
                failures.append(f"mid={mid} folder={folder}: {e}")

        assert not failures, "Attachment pipeline failures:\n" + "\n".join(failures)
        assert tested > 0, "No emails with downloadable attachments found"


class TestGmailStyleQueries:
    async def test_has_attachment(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "search", {"query": "has:attachment", "limit": 3}
        )
        data = _parse_result(result)
        assert isinstance(data, list)

    async def test_is_unread(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "search", {"query": "is:unread", "limit": 3}
        )
        data = _parse_result(result)
        assert isinstance(data, list)

    async def test_combined_gmail_query(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "search", {"query": "has:attachment is:read", "limit": 3}
        )
        data = _parse_result(result)
        assert isinstance(data, list)

    async def test_in_inbox(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "search", {"query": "in:inbox", "limit": 3}
        )
        data = _parse_result(result)
        assert isinstance(data, list)
