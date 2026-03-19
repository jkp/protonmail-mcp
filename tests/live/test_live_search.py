"""Live search tests: search, Message-ID resolution, attachment pipeline."""

import pytest
from fastmcp import Client

from tests.live.conftest import _parse_result, live, skip_no_api

pytestmark = [live, skip_no_api, pytest.mark.timeout(120)]


class TestSearch:
    async def test_search_returns_list(self, live_client: Client) -> None:
        result = await live_client.call_tool("search", {"query": "in:inbox", "limit": 5})
        data = _parse_result(result)
        assert isinstance(data, list)

    async def test_search_result_has_required_fields(self, live_client: Client) -> None:
        result = await live_client.call_tool("search", {"query": "in:inbox", "limit": 5})
        data = _parse_result(result)
        if not data:
            pytest.skip("No search results to validate")
        for item in data:
            assert "message_id" in item
            assert "folder" in item
            assert "subject" in item
            assert "date" in item
            assert "from" in item


class TestSearchReadBack:
    """Verify search results have valid Message-IDs by reading each email back."""

    @pytest.fixture
    async def inbox_search_results(self, live_client: Client) -> list:
        result = await live_client.call_tool("search", {"query": "in:inbox", "limit": 10})
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
            folder = item.get("folder", "")
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
            folder = item.get("folder", "")
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
        result = await live_client.call_tool("search", {"query": "folder:Archive", "limit": 5})
        data = _parse_result(result)
        if not data:
            pytest.skip("No emails in Archive")

        failures = []
        for item in data:
            mid = item["message_id"]
            folder = item.get("folder", "")
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
        result = await live_client.call_tool("search", {"query": "folder:Sent", "limit": 5})
        data = _parse_result(result)
        if not data:
            pytest.skip("No emails in Sent")

        failures = []
        for item in data:
            mid = item["message_id"]
            folder = item.get("folder", "")
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


class TestAttachmentPipeline:
    """End-to-end: search for email with attachment -> list attachments -> download one."""

    @pytest.mark.order(after="tests/live/test_live_write.py::TestSend::test_send_to_self")
    async def test_search_list_download_attachment(self, live_client: Client) -> None:
        """Find an email with attachments, list them, download one."""
        # 1. Search for emails with attachments
        result = await live_client.call_tool("search", {"query": "has:attachment", "limit": 10})
        data = _parse_result(result)
        if not data:
            pytest.skip("No emails with attachments in database")

        # 2. Find one that has listable attachments
        target_mid = None
        target_attachment = None
        for item in data:
            mid = item["message_id"]
            att_result = await live_client.call_tool("list_attachments", {"message_id": mid})
            att_data = _parse_result(att_result)
            if isinstance(att_data, list) and att_data:
                # Skip "not yet indexed" notes
                real_atts = [a for a in att_data if "filename" in a and a.get("size", 0) > 0]
                if real_atts:
                    target_mid = mid
                    target_attachment = real_atts[0]
                    break

        if target_mid is None:
            pytest.skip("No emails with indexed, downloadable attachments found")

        # 3. Verify attachment metadata
        assert "filename" in target_attachment
        assert "size" in target_attachment
        assert "mime_type" in target_attachment
        assert target_attachment["size"] > 0

        # 4. Download the attachment
        dl_result = await live_client.call_tool(
            "download_attachment",
            {"message_id": target_mid, "filename": target_attachment["filename"]},
        )
        # download_attachment returns list[str | Image]
        content = dl_result.content
        assert len(content) > 0, "Download returned empty content"
        # The first content item should have text (filename, size, or the content itself)
        first = content[0].text if hasattr(content[0], "text") else str(content[0])
        assert target_attachment["filename"] in first or len(first) > 10, (
            f"Download content doesn't look right: {first[:200]}"
        )


class TestGmailStyleQueries:
    async def test_has_attachment(self, live_client: Client) -> None:
        result = await live_client.call_tool("search", {"query": "has:attachment", "limit": 3})
        data = _parse_result(result)
        assert isinstance(data, list)

    async def test_is_unread(self, live_client: Client) -> None:
        result = await live_client.call_tool("search", {"query": "is:unread", "limit": 3})
        data = _parse_result(result)
        assert isinstance(data, list)

    async def test_combined_gmail_query(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "search", {"query": "has:attachment is:read", "limit": 3}
        )
        data = _parse_result(result)
        assert isinstance(data, list)

    async def test_in_inbox(self, live_client: Client) -> None:
        result = await live_client.call_tool("search", {"query": "in:inbox", "limit": 3})
        data = _parse_result(result)
        assert isinstance(data, list)
