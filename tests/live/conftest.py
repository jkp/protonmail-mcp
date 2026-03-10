"""Live integration test fixtures and helpers.

Tests in this package exercise the full stack against a running Protonmail Bridge.
They are automatically skipped when himalaya can't reach the Bridge.
"""

import asyncio
import json
import shutil
import subprocess
import uuid
from typing import Any

import pytest
from fastmcp import Client

from protonmail_mcp.server import mcp

# Unique run ID to tag all test emails for cleanup
RUN_ID = uuid.uuid4().hex[:12]
TEST_SUBJECT_PREFIX = "[MCP-TEST]"


def _himalaya_available() -> bool:
    """Check if himalaya can reach Protonmail Bridge by listing folders."""
    if not shutil.which("himalaya"):
        return False
    try:
        result = subprocess.run(
            ["himalaya", "folder", "list", "--output", "json"],
            capture_output=True,
            timeout=15,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _smtp_available() -> bool:
    """Check if SMTP is available by testing TCP connectivity to Bridge."""
    import socket

    try:
        sock = socket.create_connection(("127.0.0.1", 1025), timeout=5)
        sock.close()
        return True
    except (OSError, TimeoutError):
        return False


def _notmuch_available() -> bool:
    """Check if notmuch is installed and configured."""
    if not shutil.which("notmuch"):
        return False
    try:
        import os

        from protonmail_mcp.config import Settings

        env = None
        settings = Settings()
        if settings.notmuch_config:
            env = {**os.environ, "NOTMUCH_CONFIG": settings.notmuch_config}
        result = subprocess.run(
            ["notmuch", "count", "*"],
            capture_output=True,
            timeout=10,
            env=env,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


_HIMALAYA_OK = _himalaya_available()
_SMTP_OK = _smtp_available()
_NOTMUCH_OK = _notmuch_available()

live = pytest.mark.live
skip_no_bridge = pytest.mark.skipif(not _HIMALAYA_OK, reason="himalaya/Bridge not available")
skip_no_smtp = pytest.mark.skipif(not _SMTP_OK, reason="SMTP not available (TLS or Bridge issue)")
skip_no_notmuch = pytest.mark.skipif(not _NOTMUCH_OK, reason="notmuch not available")


def _parse_result(result: Any) -> Any:
    """Extract data from a CallToolResult."""
    if result.data and not isinstance(result.data, list):
        return result.data
    text = result.content[0].text
    return json.loads(text)


def make_subject(test_name: str) -> str:
    """Create a unique, identifiable test email subject."""
    return f"{TEST_SUBJECT_PREFIX} {RUN_ID} {test_name}"


async def poll_for_email(
    client: Client,
    subject: str,
    folder: str = "INBOX",
    timeout: float = 60.0,
    interval: float = 2.0,
) -> dict[str, Any] | None:
    """Poll list_emails until an email matching subject appears."""
    elapsed = 0.0
    while elapsed < timeout:
        result = await client.call_tool(
            "list_emails", {"folder": folder, "page_size": 50}
        )
        emails = _parse_result(result)
        for email in emails:
            if email["subject"] == subject:
                return email
        await asyncio.sleep(interval)
        elapsed += interval
    return None


@pytest.fixture
async def live_client():
    """Function-scoped MCP client for live tests. Resets account after each test."""
    from fastmcp.server.middleware import AuthMiddleware

    from protonmail_mcp.server import himalaya

    original_account = himalaya.account
    # Strip auth middleware — in-memory client has no OAuth token
    original_middleware = list(mcp.middleware)
    mcp.middleware = [m for m in mcp.middleware if not isinstance(m, AuthMiddleware)]
    try:
        async with Client(mcp) as c:
            yield c
    finally:
        mcp.middleware = original_middleware
        himalaya.account = original_account


async def cleanup_test_emails(client: Client) -> None:
    """Delete any emails with our test subject prefix + run ID."""
    folders = ["INBOX", "Sent", "Archive", "Trash"]
    for folder in folders:
        try:
            result = await client.call_tool(
                "list_emails", {"folder": folder, "page_size": 50}
            )
            emails = _parse_result(result)
            for email in emails:
                if f"{TEST_SUBJECT_PREFIX} {RUN_ID}" in email.get("subject", ""):
                    try:
                        await client.call_tool(
                            "delete", {"email_id": email["id"], "folder": folder}
                        )
                    except Exception:
                        pass
        except Exception:
            pass
