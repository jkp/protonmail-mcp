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
    """Check if himalaya can send via SMTP (Protonmail Bridge SMTP)."""
    if not shutil.which("himalaya"):
        return False
    try:
        # Try to generate a template — this doesn't actually send
        result = subprocess.run(
            ["himalaya", "template", "write"],
            input=b"To: test@test.invalid\nSubject: smtp-probe\n\nprobe",
            capture_output=True,
            timeout=15,
        )
        # template write succeeding means at least the template engine works;
        # actual SMTP is harder to probe without sending. We try a send to
        # nowhere and check if the error is TLS vs. something else.
        send_result = subprocess.run(
            ["himalaya", "template", "send"],
            input=b"To: test@test.invalid\nSubject: smtp-probe\n\nprobe",
            capture_output=True,
            timeout=15,
        )
        stderr = send_result.stderr.decode()
        # If we get a TLS error, SMTP is not available
        if "tls" in stderr.lower() or "cannot connect to smtp" in stderr.lower():
            return False
        return send_result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _notmuch_available() -> bool:
    """Check if notmuch is installed and configured."""
    if not shutil.which("notmuch"):
        return False
    try:
        result = subprocess.run(
            ["notmuch", "count", "*"],
            capture_output=True,
            timeout=10,
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
    from protonmail_mcp.server import himalaya

    original_account = himalaya.account
    async with Client(mcp) as c:
        yield c
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
