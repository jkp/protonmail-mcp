"""Live management tests (archive, move, delete) against local Maildir."""

import pytest
from fastmcp import Client

from tests.live.conftest import (
    _parse_result,
    cleanup_test_emails,
    live,
    make_subject,
    poll_for_email,
    skip_no_maildir,
    skip_no_smtp,
)

pytestmark = [live, skip_no_maildir, pytest.mark.timeout(180)]

SELF_ADDR = "jamie@kirkpatrick.email"


async def _send_and_wait(client: Client, test_name: str) -> dict:
    """Send a test email to self and wait for it to arrive."""
    subject = make_subject(test_name)
    await client.call_tool(
        "send",
        {
            "to": SELF_ADDR,
            "subject": subject,
            "body": f"Test email for {test_name}.",
        },
    )
    email = await poll_for_email(client, subject)
    assert email is not None, f"Email '{subject}' never arrived"
    return email


@skip_no_smtp
class TestArchive:
    async def test_archive_email(self, live_client: Client) -> None:
        email = await _send_and_wait(live_client, "archive")
        result = await live_client.call_tool(
            "archive", {"message_id": email["message_id"], "folder": "INBOX"}
        )
        data = _parse_result(result)
        assert data["status"] == "archived"

        await cleanup_test_emails(live_client)


@skip_no_smtp
class TestMoveEmail:
    async def test_move_to_trash(self, live_client: Client) -> None:
        email = await _send_and_wait(live_client, "move")
        result = await live_client.call_tool(
            "move_email",
            {
                "message_id": email["message_id"],
                "from_folder": "INBOX",
                "to_folder": "Trash",
            },
        )
        data = _parse_result(result)
        assert data["status"] == "moved"

        await cleanup_test_emails(live_client)


@skip_no_smtp
class TestDelete:
    async def test_delete_email(self, live_client: Client) -> None:
        email = await _send_and_wait(live_client, "delete")
        result = await live_client.call_tool(
            "delete", {"message_id": email["message_id"], "folder": "INBOX"}
        )
        data = _parse_result(result)
        assert data["status"] == "deleted"

        await cleanup_test_emails(live_client)
