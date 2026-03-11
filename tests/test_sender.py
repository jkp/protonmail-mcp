"""Tests for email_mcp.sender — SMTP sending."""

from email.message import EmailMessage
from unittest.mock import AsyncMock, patch

import pytest

from email_mcp.sender import SmtpSender


@pytest.fixture
def sender():
    return SmtpSender(
        hostname="localhost",
        port=1025,
        username="user",
        password="pass",
    )


async def test_send(sender):
    msg = EmailMessage()
    msg["From"] = "bob@example.com"
    msg["To"] = "alice@example.com"
    msg["Subject"] = "Test"
    msg.set_content("Hello")

    with patch("email_mcp.sender.aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        await sender.send(msg)
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        assert call_kwargs.kwargs["hostname"] == "localhost"
        assert call_kwargs.kwargs["port"] == 1025


async def test_send_and_save(sender, tmp_path):
    msg = EmailMessage()
    msg["From"] = "bob@example.com"
    msg["To"] = "alice@example.com"
    msg["Subject"] = "Test"
    msg.set_content("Hello")

    maildir = tmp_path / "mail"
    (maildir / "Sent" / "cur").mkdir(parents=True)

    with patch("email_mcp.sender.aiosmtplib.send", new_callable=AsyncMock):
        await sender.send_and_save(msg, maildir)

    # Verify file was saved to Sent
    sent_files = list((maildir / "Sent" / "cur").iterdir())
    assert len(sent_files) == 1
