"""Live search tests: UID resolution, read-back, and attachment pipeline."""

import pytest
from fastmcp import Client

from tests.live.conftest import _parse_result, live, skip_no_bridge, skip_no_notmuch

pytestmark = [live, skip_no_bridge, skip_no_notmuch, pytest.mark.timeout(120)]


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
            assert "uid" in item
            assert "folder" in item
            assert "subject" in item
            assert "date" in item
            assert "authors" in item


class TestSearchUidResolution:
    """Verify search results have valid IMAP UIDs by reading each email back."""

    @pytest.fixture
    async def inbox_search_results(self, live_client: Client) -> list:
        result = await live_client.call_tool(
            "search", {"query": "tag:inbox", "limit": 10}
        )
        data = _parse_result(result)
        if len(data) < 5:
            pytest.skip("Need at least 5 search results for comprehensive test")
        return data

    async def test_all_search_uids_are_readable(
        self, live_client: Client, inbox_search_results: list
    ) -> None:
        """Every search result UID must be readable via read_email."""
        failures = []
        for item in inbox_search_results:
            uid = item["uid"]
            folder = item["folder"]
            try:
                result = await live_client.call_tool(
                    "read_email", {"email_id": uid, "folder": folder}
                )
                data = _parse_result(result)
                assert data["subject"], f"uid={uid} folder={folder} has empty subject"
            except Exception as e:
                failures.append(f"uid={uid} folder={folder} subject='{item['subject']}': {e}")

        assert not failures, "Failed to read emails:\n" + "\n".join(failures)

    async def test_search_subjects_match_read_subjects(
        self, live_client: Client, inbox_search_results: list
    ) -> None:
        """Subject from search must match subject from read_email."""
        mismatches = []
        for item in inbox_search_results:
            uid = item["uid"]
            folder = item["folder"]
            try:
                result = await live_client.call_tool(
                    "read_email", {"email_id": uid, "folder": folder}
                )
                data = _parse_result(result)
                if data["subject"] != item["subject"]:
                    mismatches.append(
                        f"uid={uid}: search='{item['subject']}' vs read='{data['subject']}'"
                    )
            except Exception as e:
                mismatches.append(f"uid={uid}: read failed: {e}")

        assert not mismatches, "Subject mismatches (UID wrong?):\n" + "\n".join(mismatches)


class TestSearchAcrossFolders:
    """Verify UID resolution works for emails in different folders."""

    async def test_archive_emails_are_readable(self, live_client: Client) -> None:
        """Search Archive folder and verify UIDs resolve correctly."""
        result = await live_client.call_tool(
            "search", {"query": "folder:Archive", "limit": 5}
        )
        data = _parse_result(result)
        if not data:
            pytest.skip("No emails in Archive")

        failures = []
        for item in data:
            uid = item["uid"]
            folder = item["folder"]
            assert folder == "Archive", f"Expected Archive, got {folder}"
            try:
                result = await live_client.call_tool(
                    "read_email", {"email_id": uid, "folder": folder}
                )
                read_data = _parse_result(result)
                if read_data["subject"] != item["subject"]:
                    failures.append(
                        f"uid={uid}: search='{item['subject']}' vs read='{read_data['subject']}'"
                    )
            except Exception as e:
                failures.append(f"uid={uid} subject='{item['subject']}': {e}")

        assert not failures, "Archive UID resolution failures:\n" + "\n".join(failures)

    async def test_sent_emails_are_readable(self, live_client: Client) -> None:
        """Search Sent folder and verify UIDs resolve correctly."""
        result = await live_client.call_tool(
            "search", {"query": "folder:Sent", "limit": 5}
        )
        data = _parse_result(result)
        if not data:
            pytest.skip("No emails in Sent")

        failures = []
        for item in data:
            uid = item["uid"]
            folder = item["folder"]
            try:
                result = await live_client.call_tool(
                    "read_email", {"email_id": uid, "folder": folder}
                )
                _parse_result(result)
            except Exception as e:
                failures.append(f"uid={uid} subject='{item['subject']}': {e}")

        assert not failures, "Sent UID resolution failures:\n" + "\n".join(failures)


class TestSearchAttachmentPipeline:
    """End-to-end: search for emails with attachments → list → download."""

    async def test_attachment_emails_have_listable_attachments(
        self, live_client: Client
    ) -> None:
        """Search for emails tagged with attachment, verify list_attachments works."""
        result = await live_client.call_tool(
            "search", {"query": "tag:attachment", "limit": 5}
        )
        data = _parse_result(result)
        if not data:
            pytest.skip("No emails with attachment tag")

        tested = 0
        failures = []
        for item in data:
            uid = item["uid"]
            folder = item["folder"]
            try:
                att_result = await live_client.call_tool(
                    "list_attachments", {"email_id": uid, "folder": folder}
                )
                att_data = _parse_result(att_result)
                if att_data:
                    tested += 1
                    for att in att_data:
                        assert "filename" in att, f"uid={uid}: attachment missing filename"
                        assert "size" in att, f"uid={uid}: attachment missing size"
                        assert att["size"] > 0, f"uid={uid}: attachment size is 0"
            except Exception as e:
                failures.append(f"uid={uid} folder={folder} subject='{item['subject']}': {e}")

        assert not failures, "Attachment pipeline failures:\n" + "\n".join(failures)
        assert tested > 0, "No emails with downloadable attachments found"

    async def test_download_first_attachment(self, live_client: Client) -> None:
        """Find an email with attachments and download the first one."""
        result = await live_client.call_tool(
            "search", {"query": "tag:attachment", "limit": 10}
        )
        data = _parse_result(result)
        if not data:
            pytest.skip("No emails with attachment tag")

        for item in data:
            uid = item["uid"]
            folder = item["folder"]
            att_result = await live_client.call_tool(
                "list_attachments", {"email_id": uid, "folder": folder}
            )
            att_data = _parse_result(att_result)
            if att_data:
                filename = att_data[0]["filename"]
                dl_result = await live_client.call_tool(
                    "download_attachment",
                    {"email_id": uid, "folder": folder, "filename": filename},
                )
                assert dl_result.content, f"Empty download for {filename}"
                assert len(dl_result.content) > 0
                return

        pytest.skip("No downloadable attachments found in search results")


class TestGmailStyleQueries:
    """Verify Gmail-style query translation works end-to-end."""

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
