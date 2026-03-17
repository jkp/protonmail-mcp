"""Live cleanup: runs last to clean up all test emails via IMAP.

Exercises the query-based batch tools (search_and_mark_read,
search_and_delete) while cleaning up [MCP-TEST] emails created
by earlier tests.

Uses @pytest.mark.order("last") to ensure this runs after all
other live tests regardless of file naming.
"""

import pytest
from fastmcp import Client

from tests.live.conftest import (
    TEST_SUBJECT_PREFIX,
    _parse_result,
    live,
    skip_no_maildir,
    skip_no_notmuch,
)

pytestmark = [
    live,
    skip_no_maildir,
    skip_no_notmuch,
    pytest.mark.timeout(120),
    pytest.mark.order("last"),
]

# Quote the brackets to avoid luqum parsing them as range syntax
TEST_QUERY = f'subject:"{TEST_SUBJECT_PREFIX}"'


class TestCleanupMarkRead:
    """Mark all test emails as read before deletion."""

    async def test_sync_before_cleanup(self, live_client: Client) -> None:
        """Sync so notmuch can find all test emails from this run."""
        result = await live_client.call_tool("sync_now", {})
        data = _parse_result(result)
        assert data.get("status") != "error"

    async def test_dry_run(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "search_and_mark_read",
            {"query": TEST_QUERY, "dry_run": True},
        )
        data = _parse_result(result)
        assert "would_affect" in data
        assert isinstance(data["would_affect"], int)
        assert "by_folder" in data
        for subj in data.get("sample_subjects", []):
            assert TEST_SUBJECT_PREFIX in subj

    async def test_execute(self, live_client: Client) -> None:
        dry = await live_client.call_tool(
            "search_and_mark_read",
            {"query": TEST_QUERY, "dry_run": True},
        )
        dry_data = _parse_result(dry)
        if dry_data["would_affect"] == 0:
            pytest.skip("No test emails to mark as read")

        result = await live_client.call_tool(
            "search_and_mark_read",
            {"query": TEST_QUERY, "dry_run": False},
        )
        data = _parse_result(result)
        assert "succeeded" in data
        assert "failed" in data
        assert data["succeeded"] >= 1


class TestCleanupDelete:
    """Delete all test emails — the final cleanup step."""

    async def test_dry_run(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "search_and_delete",
            {"query": TEST_QUERY, "dry_run": True},
        )
        data = _parse_result(result)
        assert "would_affect" in data
        assert isinstance(data["would_affect"], int)
        assert "by_folder" in data
        for subj in data.get("sample_subjects", []):
            assert TEST_SUBJECT_PREFIX in subj

    async def test_execute(self, live_client: Client) -> None:
        dry = await live_client.call_tool(
            "search_and_delete",
            {"query": TEST_QUERY, "dry_run": True},
        )
        dry_data = _parse_result(dry)
        if dry_data["would_affect"] == 0:
            pytest.skip("No test emails to delete")

        result = await live_client.call_tool(
            "search_and_delete",
            {"query": TEST_QUERY, "dry_run": False},
        )
        data = _parse_result(result)
        assert "succeeded" in data
        assert "failed" in data
        # succeeded+failed may exceed would_affect: self-sent messages appear in
        # multiple folders (INBOX + Sent) and are attempted in each separately.
        assert data["succeeded"] >= 1

