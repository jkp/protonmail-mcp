"""Composing tools: send, reply, forward using ProtonMail API."""

from datetime import UTC
from email.message import EmailMessage
from typing import Any

import structlog

from email_mcp.composer import build_forward, build_new, build_reply
from email_mcp.db import resolve_message
from email_mcp.models import Address
from email_mcp.sender import ProtonSender
from email_mcp.server import db, mcp, settings

logger = structlog.get_logger()

# Module-level ref — set during server lifespan
_sender: ProtonSender | None = None


def _from_address() -> Address:
    return Address(name=settings.from_name, addr=settings.from_address)


def _build_original_email(identifier: int | str) -> tuple[EmailMessage, str] | tuple[None, str]:
    """Build an EmailMessage from SQLite data for reply/forward composition.

    Returns (EmailMessage, pm_id) or (None, "") if not found.
    """
    msg_row = resolve_message(db, identifier)
    if msg_row is None:
        return None, ""
    body_text = db.bodies.get(msg_row.pm_id) or ""

    # Construct a minimal EmailMessage with headers the composer needs
    email = EmailMessage()
    email["From"] = (
        f"{msg_row.sender_name} <{msg_row.sender_email}>"
        if msg_row.sender_name
        else msg_row.sender_email
    )
    to_addrs = ", ".join(
        f"{r['name']} <{r['email']}>" if r.get("name") else r["email"] for r in msg_row.recipients
    )
    email["To"] = to_addrs
    email["Subject"] = msg_row.subject or ""
    if msg_row.message_id:
        email["Message-ID"] = f"<{msg_row.message_id}>"
    from datetime import datetime

    email["Date"] = datetime.fromtimestamp(msg_row.date, tz=UTC).strftime(
        "%a, %d %b %Y %H:%M:%S %z"
    )
    # Strip HTML tags for plain text quoting
    from email_mcp.convert import body_for_display

    body_text = body_for_display(body_text)
    email.set_content(body_text)

    return email, msg_row.pm_id


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
    if _sender is None:
        return {"error": "Sender not initialized — ProtonMail API not available."}
    sender = _resolve_from(from_address)
    logger.info("tool.send", to=to, subject=subject, cc=cc, from_=sender.addr)
    try:
        msg = build_new(sender, to, subject, body, cc)
        await _sender.send(msg)
    except Exception as e:
        logger.error("tool.send.failed", to=to, error=str(e), exc_info=True)
        return {"error": f"Send failed: {e}"}
    logger.info("tool.send.done", to=to, subject=subject)
    return {"status": "sent", "to": to, "subject": subject}


@mcp.tool(annotations={"destructiveHint": False, "title": "Reply to Email"})
async def reply(
    id: int | str | None = None,
    message_id: str | None = None,
    body: str = "",
    folder: str | None = None,
    reply_all: bool = False,
    from_address: str | None = None,
) -> dict[str, Any]:
    """Reply to an email.

    Args:
        id: Numeric message id (from search or list results)
        message_id: Legacy message_id or pm_id string (use id instead)
        body: Reply body text
        folder: Optional folder hint
        reply_all: Whether to reply to all recipients
        from_address: Sender email address (defaults to configured from_address)
    """
    id = id or message_id
    if id is None:
        return {"error": "missing_id", "detail": "Provide id or message_id."}
    if _sender is None:
        return {"error": "Sender not initialized — ProtonMail API not available."}
    sender = _resolve_from(from_address)
    logger.info("tool.reply", id=id, reply_all=reply_all, from_=sender.addr)

    original, pm_id = _build_original_email(id)
    if original is None:
        return {"error": f"Email not found: {id}"}

    try:
        msg = build_reply(original, body, sender, reply_all)
        await _sender.send(
            msg,
            parent_id=pm_id,
            action=1 if reply_all else 0,
        )
    except Exception as e:
        logger.error("tool.reply.failed", id=id, error=str(e), exc_info=True)
        return {"error": f"Reply failed: {e}"}
    logger.info("tool.reply.done", id=id)
    return {"status": "sent", "in_reply_to": id}


@mcp.tool(annotations={"destructiveHint": False, "title": "Forward Email"})
async def forward(
    id: int | str | None = None,
    message_id: str | None = None,
    to: str = "",
    body: str = "",
    folder: str | None = None,
    from_address: str | None = None,
) -> dict[str, Any]:
    """Forward an email to another recipient.

    Args:
        id: Numeric message id (from search or list results)
        message_id: Legacy message_id or pm_id string (use id instead)
        to: Recipient email address
        body: Additional body text to prepend
        folder: Optional folder hint
        from_address: Sender email address (defaults to configured from_address)
    """
    id = id or message_id
    if id is None:
        return {"error": "missing_id", "detail": "Provide id or message_id."}
    if _sender is None:
        return {"error": "Sender not initialized — ProtonMail API not available."}
    sender = _resolve_from(from_address)
    logger.info("tool.forward", id=id, to=to, from_=sender.addr)

    original, pm_id = _build_original_email(id)
    if original is None:
        return {"error": f"Email not found: {id}"}

    # Download original attachments for forwarding
    attachments: list[tuple[str, str, bytes]] = []
    if pm_id:
        from email_mcp.tools.reading import _decryptor

        att_list = db.attachments.list_for_message(pm_id)
        if att_list and _decryptor:
            for att in att_list:
                if att.get("att_id"):
                    try:
                        content = await _decryptor.fetch_attachment(
                            att["att_id"], att.get("key_packets", "")
                        )
                        attachments.append((att["filename"], att["mime_type"], content))
                        logger.info("tool.forward.attachment", filename=att["filename"])
                    except Exception as e:
                        logger.warning(
                            "tool.forward.attachment_failed",
                            filename=att["filename"],
                            error=str(e),
                        )

    try:
        msg = build_forward(original, to, body, sender)
        await _sender.send(
            msg,
            attachments=attachments or None,
            parent_id=pm_id,
            action=2,  # Forward
        )
    except Exception as e:
        logger.error("tool.forward.failed", id=id, error=str(e), exc_info=True)
        return {"error": f"Forward failed: {e}"}
    logger.info(
        "tool.forward.done",
        id=id,
        to=to,
        attachments=len(attachments),
    )
    return {"status": "sent", "forwarded_to": to, "original_id": id}
