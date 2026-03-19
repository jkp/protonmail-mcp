"""Live write tests (send, reply, forward) against Protonmail Bridge."""

import pytest
from fastmcp import Client

from tests.live.conftest import (
    _parse_result,
    live,
    make_subject,
    poll_for_email,
    skip_no_api,
)

pytestmark = [live, skip_no_api, skip_no_api, pytest.mark.timeout(180)]

SELF_ADDR = "jamie@kirkpatrick.email"


class TestSend:
    async def test_send_to_self(self, live_client: Client) -> None:
        subject = make_subject("send_to_self")
        result = await live_client.call_tool(
            "send",
            {
                "to": SELF_ADDR,
                "subject": subject,
                "body": "Live test: send to self.",
            },
        )
        data = _parse_result(result)
        assert data["status"] == "sent"

        email = await poll_for_email(live_client, subject)
        assert email is not None, f"Email '{subject}' never arrived in INBOX"


class TestReply:
    async def test_reply_to_self(self, live_client: Client) -> None:
        subject = make_subject("reply_original")
        await live_client.call_tool(
            "send",
            {
                "to": SELF_ADDR,
                "subject": subject,
                "body": "Original message for reply test.",
            },
        )
        email = await poll_for_email(live_client, subject)
        assert email is not None, f"Original email '{subject}' never arrived"

        result = await live_client.call_tool(
            "reply",
            {
                "message_id": email["message_id"],
                "body": "This is a reply.",
                "folder": "INBOX",
            },
        )
        data = _parse_result(result)
        assert data["status"] == "sent"
        assert data["in_reply_to"] == email["message_id"]


class TestForward:
    async def test_forward_to_self(self, live_client: Client) -> None:
        subject = make_subject("forward_original")
        await live_client.call_tool(
            "send",
            {
                "to": SELF_ADDR,
                "subject": subject,
                "body": "Original message for forward test.",
            },
        )
        email = await poll_for_email(live_client, subject)
        assert email is not None, f"Original email '{subject}' never arrived"

        result = await live_client.call_tool(
            "forward",
            {
                "message_id": email["message_id"],
                "to": SELF_ADDR,
                "body": "Forwarding this to you.",
                "folder": "INBOX",
            },
        )
        data = _parse_result(result)
        assert data["status"] == "sent"
        assert data["forwarded_to"] == SELF_ADDR
