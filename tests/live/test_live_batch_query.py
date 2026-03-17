"""Live tests for query-based batch tools (search_and_mark_read, search_and_delete).

These tests use the [MCP-TEST] subject prefix to find leftover test emails,
exercise the dry_run flow, then clean them up via search_and_delete.
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

pytestmark = [live, skip_no_maildir, skip_no_notmuch, pytest.mark.timeout(120)]

# Query that matches all test emails across all folders
# Quote the brackets to avoid luqum parsing them as range syntax
TEST_QUERY = f'subject:"{TEST_SUBJECT_PREFIX}"'


class TestSearchAndMarkRead:
    async def test_dry_run_finds_test_emails(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "search_and_mark_read",
            {"query": TEST_QUERY, "dry_run": True},
        )
        data = _parse_result(result)
        assert "would_affect" in data
        assert isinstance(data["would_affect"], int)
        assert "sample_subjects" in data
        assert isinstance(data["sample_subjects"], list)
        # All sample subjects should contain our test prefix
        for subj in data["sample_subjects"]:
            assert TEST_SUBJECT_PREFIX in subj

    async def test_execute_marks_read(self, live_client: Client) -> None:
        # Dry run first to see what we're working with
        dry = await live_client.call_tool(
            "search_and_mark_read",
            {"query": TEST_QUERY, "dry_run": True},
        )
        dry_data = _parse_result(dry)
        if dry_data["would_affect"] == 0:
            pytest.skip("No test emails to mark as read")

        # Execute
        result = await live_client.call_tool(
            "search_and_mark_read",
            {"query": TEST_QUERY, "dry_run": False},
        )
        data = _parse_result(result)
        assert "succeeded" in data
        assert "failed" in data
        assert "errors" in data
        assert data["succeeded"] + data["failed"] == dry_data["would_affect"]


class TestSearchAndDelete:
    async def test_dry_run_finds_test_emails(self, live_client: Client) -> None:
        result = await live_client.call_tool(
            "search_and_delete",
            {"query": TEST_QUERY, "dry_run": True},
        )
        data = _parse_result(result)
        assert "would_affect" in data
        assert isinstance(data["would_affect"], int)
        assert "sample_subjects" in data
        for subj in data["sample_subjects"]:
            assert TEST_SUBJECT_PREFIX in subj

    async def test_execute_deletes_test_emails(self, live_client: Client) -> None:
        # Dry run first
        dry = await live_client.call_tool(
            "search_and_delete",
            {"query": TEST_QUERY, "dry_run": True},
        )
        dry_data = _parse_result(dry)
        if dry_data["would_affect"] == 0:
            pytest.skip("No test emails to delete")

        # Execute
        result = await live_client.call_tool(
            "search_and_delete",
            {"query": TEST_QUERY, "dry_run": False},
        )
        data = _parse_result(result)
        assert "succeeded" in data
        assert "failed" in data
        assert "errors" in data
        assert data["succeeded"] + data["failed"] == dry_data["would_affect"]

        # Verify: dry run should now find fewer (ideally zero)
        verify = await live_client.call_tool(
            "search_and_delete",
            {"query": TEST_QUERY, "dry_run": True},
        )
        verify_data = _parse_result(verify)
        # Some may still show in stale notmuch index, but count should be reduced
        assert verify_data["would_affect"] <= dry_data["would_affect"]
