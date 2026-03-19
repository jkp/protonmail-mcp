"""Live integration test fixtures and helpers.

Tests exercise the full stack against ProtonMail API.
Auto-skipped when session file not available.
"""

import asyncio
import json
import uuid
from typing import Any

import pytest
from fastmcp import Client

from email_mcp.config import Settings

# Unique run ID to tag all test emails for cleanup
RUN_ID = uuid.uuid4().hex[:12]
TEST_SUBJECT_PREFIX = "[MCP-TEST]"


def _api_session_available() -> bool:
    """Check if a ProtonMail session file exists."""
    try:
        settings = Settings()
        return settings.proton_session_file.exists()
    except Exception:
        return False


_API_OK = _api_session_available()

live = pytest.mark.live
skip_no_api = pytest.mark.skipif(not _API_OK, reason="ProtonMail session not available")


def _parse_result(result: Any) -> Any:
    """Extract data from a CallToolResult."""
    if result.data and not isinstance(result.data, list):
        return result.data
    text = result.content[0].text
    return json.loads(text)


def _parse_emails(result: Any) -> list[dict[str, Any]]:
    """Extract the email list from a list_emails CallToolResult.

    list_emails returns a paginated dict {"emails": [...], "total": N, "count": N}.
    """
    data = _parse_result(result)
    if isinstance(data, dict):
        return data.get("emails", [])
    return data


def make_subject(test_name: str) -> str:
    """Create a unique, identifiable test email subject."""
    return f"{TEST_SUBJECT_PREFIX} {RUN_ID} {test_name}"


async def poll_for_email(
    client: Client,
    subject: str,
    folder: str = "INBOX",
    timeout: float = 90.0,
    interval: float = 5.0,
) -> dict[str, Any] | None:
    """Poll until an email matching subject appears in local DB.

    Triggers sync_now each iteration to pull new events from ProtonMail,
    then searches by subject to find the email regardless of page position.
    """
    elapsed = 0.0
    while elapsed < timeout:
        # Trigger event poll to pick up newly sent/received emails
        try:
            await client.call_tool("sync_now", {})
        except Exception:
            pass

        # Search by subject — more reliable than paging through list_emails
        result = await client.call_tool("search", {"query": f'subject:"{subject}"', "limit": 5})
        data = _parse_result(result)
        if isinstance(data, list):
            for email in data:
                if email.get("subject") == subject:
                    return email

        await asyncio.sleep(interval)
        elapsed += interval
    return None


@pytest.fixture
async def live_client():
    """Function-scoped MCP client for live tests.

    Each test gets a fresh server lifespan. Teardown errors from background
    task cancellation are suppressed — they're noise from the anyio task group
    racing with the lifespan shutdown.
    """
    from email_mcp.server import mcp

    # Strip auth middleware for in-memory client
    try:
        from fastmcp.server.middleware import AuthMiddleware

        original_middleware = list(mcp.middleware)
        mcp.middleware = [m for m in mcp.middleware if not isinstance(m, AuthMiddleware)]
    except ImportError:
        original_middleware = None

    try:
        async with Client(mcp) as c:
            yield c
    except (asyncio.CancelledError, Exception):
        # Suppress teardown noise from background task cancellation.
        # The server lifespan cancels event_loop, embedder, body_indexer tasks
        # on shutdown, and anyio's task group can propagate errors during cleanup.
        pass
    finally:
        if original_middleware is not None:
            mcp.middleware = original_middleware
