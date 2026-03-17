"""Read email and download attachment tools."""

import base64
import mimetypes
from datetime import datetime, timezone
from typing import Any

import structlog
from fastmcp.utilities.types import Image

from email_mcp.server import db, mcp

logger = structlog.get_logger()

_TEXT_EXTENSIONS = {
    ".txt", ".csv", ".json", ".xml", ".html", ".htm", ".md", ".yaml", ".yml", ".log",
}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _format_date(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "Read Email"}
)
async def read_email(message_id: str, folder: str | None = None) -> dict[str, Any]:
    """Read a full email message by Message-ID.

    Args:
        message_id: The Message-ID header value (from search or list results)
        folder: Optional folder hint (unused, kept for API compatibility)
    """
    logger.info("tool.read_email", message_id=message_id)

    # Look up by RFC 2822 Message-ID first, fall back to pm_id
    row = db.execute(
        "SELECT * FROM messages WHERE message_id = ? OR pm_id = ?",
        [message_id, message_id],
    ).fetchone()

    if row is None:
        return {
            "error": "not_found",
            "message_id": message_id,
            "detail": "Message not found in local database. May not have been synced yet.",
        }

    from email_mcp.db import _row_to_message
    msg = _row_to_message(row)

    body = db.bodies.get(msg.pm_id) or ""

    logger.info(
        "tool.read_email.done",
        pm_id=msg.pm_id,
        subject=msg.subject,
        body_len=len(body),
        body_indexed=msg.body_indexed,
    )

    return {
        "message_id": msg.message_id,
        "pm_id": msg.pm_id,
        "from": f"{msg.sender_name} <{msg.sender_email}>" if msg.sender_name else msg.sender_email,
        "to": msg.recipients,
        "subject": msg.subject,
        "date": _format_date(msg.date),
        "body": body,
        "folder": msg.folder,
        "unread": msg.unread,
        "has_attachments": msg.has_attachments,
        "body_indexed": msg.body_indexed,
    }


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "List Attachments"}
)
async def list_attachments(message_id: str, folder: str | None = None) -> list[dict[str, Any]]:
    """List attachments on an email without downloading them.

    Note: Attachment metadata is not yet available in v4. This will be populated
    once the attachment indexing phase is complete.

    Args:
        message_id: The Message-ID header value
        folder: Optional folder hint (unused)
    """
    logger.info("tool.list_attachments", message_id=message_id)

    row = db.execute(
        "SELECT has_attachments FROM messages WHERE message_id = ? OR pm_id = ?",
        [message_id, message_id],
    ).fetchone()

    if row is None or not row[0]:
        return []

    # Attachment detail metadata not yet in SQLite — return placeholder
    return [{"note": "Attachment metadata will be available in a future sync"}]


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "Download Attachment"}
)
async def download_attachment(
    message_id: str,
    filename: str,
    folder: str | None = None,
) -> list[str | Image]:
    """Download and return an email attachment.

    Note: Attachment download is not yet implemented in v4.

    Args:
        message_id: The Message-ID header value
        filename: The attachment filename to download
        folder: Optional folder hint
    """
    logger.info("tool.download_attachment", message_id=message_id, filename=filename)
    return ["Attachment download is not yet implemented in v4 architecture."]
