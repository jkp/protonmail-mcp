"""Live integration test fixtures and helpers.

Tests exercise the full stack against a running Protonmail Bridge
via local Maildir + notmuch + aiosmtplib. Auto-skipped when
Bridge/mbsync/notmuch not available.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import uuid
from typing import Any

import pytest
from fastmcp import Client

from email_mcp.config import Settings

# Unique run ID to tag all test emails for cleanup
RUN_ID = uuid.uuid4().hex[:12]
TEST_SUBJECT_PREFIX = "[MCP-TEST]"


def _notmuch_available() -> bool:
    """Check if notmuch is installed and configured."""
    if not shutil.which("notmuch"):
        return False
    try:
        settings = Settings()
        maildir = settings.maildir_path
        config_path = maildir / ".notmuch" / "config"
        env = {**os.environ}
        if config_path.exists():
            env["NOTMUCH_CONFIG"] = str(config_path)
        result = subprocess.run(
            ["notmuch", "count", "*"],
            capture_output=True,
            timeout=10,
            env=env,
        )
        return result.returncode == 0
    except Exception:
        return False


def _smtp_available() -> bool:
    """Check if SMTP is available by testing TCP connectivity to Bridge."""
    try:
        settings = Settings()
        sock = socket.create_connection(
            (settings.smtp_host, settings.smtp_port), timeout=5
        )
        sock.close()
        return True
    except (OSError, TimeoutError):
        return False


def _maildir_available() -> bool:
    """Check if the Maildir exists and has mail."""
    try:
        settings = Settings()
        maildir = settings.maildir_path
        return (maildir / "INBOX" / "cur").is_dir()
    except Exception:
        return False


_NOTMUCH_OK = _notmuch_available()
_SMTP_OK = _smtp_available()
_MAILDIR_OK = _maildir_available()

live = pytest.mark.live
skip_no_maildir = pytest.mark.skipif(
    not _MAILDIR_OK, reason="Maildir not available"
)
skip_no_smtp = pytest.mark.skipif(
    not _SMTP_OK, reason="SMTP not available"
)
skip_no_notmuch = pytest.mark.skipif(
    not _NOTMUCH_OK, reason="notmuch not available"
)


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
    """Poll list_emails until an email matching subject appears.

    After each failed check, triggers mbsync + notmuch new to pull new mail.
    """
    elapsed = 0.0
    settings = Settings()
    maildir = settings.maildir_path
    notmuch_config = maildir / ".notmuch" / "config"

    while elapsed < timeout:
        # Sync to pull any new mail
        try:
            subprocess.run(["mbsync", "-a"], capture_output=True, timeout=30)
            env = {**os.environ}
            if notmuch_config.exists():
                env["NOTMUCH_CONFIG"] = str(notmuch_config)
            subprocess.run(["notmuch", "new"], capture_output=True, timeout=15, env=env)
        except Exception:
            pass

        result = await client.call_tool(
            "list_emails", {"folder": folder, "limit": 50}
        )
        data = _parse_result(result)
        emails = data.get("emails", []) if isinstance(data, dict) else data
        for email in emails:
            if email.get("subject") == subject:
                return email
        await asyncio.sleep(interval)
        elapsed += interval
    return None


@pytest.fixture
async def live_client():
    """Function-scoped MCP client for live tests."""
    from email_mcp.server import mcp

    # Strip auth middleware for in-memory client
    try:
        from fastmcp.server.middleware import AuthMiddleware

        original_middleware = list(mcp.middleware)
        mcp.middleware = [
            m for m in mcp.middleware
            if not isinstance(m, AuthMiddleware)
        ]
    except ImportError:
        original_middleware = None

    try:
        async with Client(mcp) as c:
            yield c
    finally:
        if original_middleware is not None:
            mcp.middleware = original_middleware


