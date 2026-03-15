"""Composing tools: send, reply, forward using stdlib email + aiosmtplib."""

from pathlib import Path
from typing import Any

import structlog

from email_mcp.composer import build_forward, build_new, build_reply
from email_mcp.models import Address
from email_mcp.sender import SmtpSender
from email_mcp.server import mcp, settings, store
from email_mcp.tools.searching import _searcher

logger = structlog.get_logger()

_sender = SmtpSender(
    hostname=settings.smtp_host,
    port=settings.smtp_port,
    username=settings.smtp_username,
    password=settings.smtp_password,
    start_tls=settings.smtp_starttls,
    cert_path=settings.smtp_cert_path,
)


def _from_address() -> Address:
    return Address(name=settings.from_name, addr=settings.from_address)


async def _find_original(message_id: str, folder: str | None = None) -> Path | None:
    """Find the file path for a message, using notmuch fast path first."""
    path_str = await _searcher.find_message_path(message_id)
    if path_str:
        p = Path(path_str)
        if p.exists():
            return p
    # Slow fallback
    return store._find_file_by_message_id(message_id, folder)


def _resolve_from(from_address: str | None = None) -> Address:
    """Resolve the sender address, using override or default."""
    if from_address:
        return Address(name=settings.from_name, addr=from_address)
    return _from_address()


@mcp.tool(annotations={"destructiveHint": False, "title": "Send Email"})
async def send(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    from_address: str | None = None,
) -> dict[str, Any]:
    """Send a new email.

    Args:
        to: Recipient email address
        subject: Email subject
        body: Email body text
        cc: CC recipients (comma-separated)
        from_address: Sender email address (defaults to configured from_address)
    """
    sender = _resolve_from(from_address)
    logger.info("tool.send", to=to, subject=subject, cc=cc, from_=sender.addr)
    msg = build_new(sender, to, subject, body, cc)
    await _sender.send_and_save(msg, settings.maildir_path)
    logger.info("tool.send.done", to=to, subject=subject)
    return {"status": "sent", "to": to, "subject": subject}


@mcp.tool(annotations={"destructiveHint": False, "title": "Reply to Email"})
async def reply(
    message_id: str,
    body: str,
    folder: str | None = None,
    reply_all: bool = False,
    from_address: str | None = None,
) -> dict[str, Any]:
    """Reply to an email.

    Args:
        message_id: The Message-ID of the email to reply to
        body: Reply body text
        folder: Optional folder hint
        reply_all: Whether to reply to all recipients
        from_address: Sender email address (defaults to configured from_address)
    """
    sender = _resolve_from(from_address)
    logger.info("tool.reply", message_id=message_id, reply_all=reply_all, from_=sender.addr)

    path = await _find_original(message_id, folder)
    if path is None:
        return {"error": f"Email not found: {message_id}"}

    original = store._parse_file(path)
    if original is None:
        return {"error": f"Could not parse email: {message_id}"}

    msg = build_reply(original, body, sender, reply_all)
    await _sender.send_and_save(msg, settings.maildir_path)
    logger.info("tool.reply.done", message_id=message_id)
    return {"status": "sent", "in_reply_to": message_id}


@mcp.tool(annotations={"destructiveHint": False, "title": "Forward Email"})
async def forward(
    message_id: str,
    to: str,
    body: str,
    folder: str | None = None,
    from_address: str | None = None,
) -> dict[str, Any]:
    """Forward an email to another recipient.

    Args:
        message_id: The Message-ID of the email to forward
        to: Recipient email address
        body: Additional body text to prepend
        folder: Optional folder hint
        from_address: Sender email address (defaults to configured from_address)
    """
    sender = _resolve_from(from_address)
    logger.info("tool.forward", message_id=message_id, to=to, from_=sender.addr)

    path = await _find_original(message_id, folder)
    if path is None:
        return {"error": f"Email not found: {message_id}"}

    original = store._parse_file(path)
    if original is None:
        return {"error": f"Could not parse email: {message_id}"}

    msg = build_forward(original, to, body, sender)
    await _sender.send_and_save(msg, settings.maildir_path)
    logger.info("tool.forward.done", message_id=message_id, to=to)
    return {"status": "sent", "forwarded_to": to, "original_id": message_id}
