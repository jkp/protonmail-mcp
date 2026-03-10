"""Live search tests using notmuch against real Maildir."""

import pytest
from fastmcp import Client

from tests.live.conftest import _parse_result, live, skip_no_bridge, skip_no_notmuch

pytestmark = [live, skip_no_bridge, skip_no_notmuch, pytest.mark.timeout(30)]


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
