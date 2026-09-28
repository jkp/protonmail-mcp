"""Tests for email_mcp.tools.composing — attachments on send/reply."""

import base64
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from email_mcp.db import Database, MessageRow
from email_mcp.models import OutgoingAttachment


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    d.messages.upsert(
        MessageRow(
            pm_id="pm-001",
            message_id="msg1@example.com",
            subject="Hello",
            sender_name="Alice",
            sender_email="alice@example.com",
            recipients=[],
            date=int(time.time()),
            unread=False,
            label_ids=["0"],
            folder="INBOX",
            size=512,
            has_attachments=False,
            body_indexed=True,
        )
    )
    return d


@pytest.fixture
def sender():
    s = MagicMock()
    s.send = AsyncMock()
    return s


@pytest.fixture(autouse=True)
def patch_composing(db, sender):
    with (
        patch("email_mcp.tools.composing.db", db),
        patch("email_mcp.tools.composing._sender", sender),
    ):
        yield


async def test_send_passes_decoded_attachments(sender):
    from email_mcp.tools.composing import send

    result = await send(
        to="bob@example.com",
        subject="Report",
        body="See attached",
        attachments=[
            OutgoingAttachment(filename="report.pdf", content_base64=_b64(b"%PDF-1.4")),
            OutgoingAttachment(
                filename="data", content_base64=_b64(b"a,b\n1,2"), mime_type="text/csv"
            ),
        ],
    )

    assert result["status"] == "sent"
    assert result["attachments"] == ["report.pdf", "data"]
    assert sender.send.await_args.kwargs["attachments"] == [
        ("report.pdf", "application/pdf", b"%PDF-1.4"),
        ("data", "text/csv", b"a,b\n1,2"),
    ]


async def test_send_unknown_extension_defaults_to_octet_stream(sender):
    from email_mcp.tools.composing import send

    await send(
        to="bob@example.com",
        subject="x",
        body="x",
        attachments=[OutgoingAttachment(filename="blob", content_base64=_b64(b"\x00\x01"))],
    )

    assert sender.send.await_args.kwargs["attachments"] == [
        ("blob", "application/octet-stream", b"\x00\x01")
    ]


async def test_send_without_attachments_passes_none(sender):
    from email_mcp.tools.composing import send

    await send(to="bob@example.com", subject="x", body="x")

    assert sender.send.await_args.kwargs["attachments"] is None


async def test_send_rejects_invalid_base64_without_sending(sender):
    from email_mcp.tools.composing import send

    result = await send(
        to="bob@example.com",
        subject="x",
        body="x",
        attachments=[OutgoingAttachment(filename="a.txt", content_base64="not base64!!")],
    )

    assert "a.txt" in result["error"]
    sender.send.assert_not_awaited()


async def test_reply_passes_attachments_and_threading(sender):
    from email_mcp.tools.composing import reply

    result = await reply(
        id="pm-001",
        body="Here you go",
        attachments=[OutgoingAttachment(filename="notes.txt", content_base64=_b64(b"hi"))],
    )

    assert result["status"] == "sent"
    kwargs = sender.send.await_args.kwargs
    assert kwargs["parent_id"] == "pm-001"
    assert kwargs["attachments"] == [("notes.txt", "text/plain", b"hi")]


async def test_reply_rejects_invalid_base64_without_sending(sender):
    from email_mcp.tools.composing import reply

    result = await reply(
        id="pm-001",
        body="x",
        attachments=[OutgoingAttachment(filename="a.txt", content_base64="%%%")],
    )

    assert "a.txt" in result["error"]
    sender.send.assert_not_awaited()
