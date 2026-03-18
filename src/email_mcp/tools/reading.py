"""Read email and download attachment tools."""

import base64
import mimetypes
from datetime import datetime, timezone
from typing import Any

import structlog
from fastmcp.utilities.types import Image

from email_mcp.decryptor import ProtonDecryptor
from email_mcp.imap import ImapMutator
from email_mcp.server import db, mcp

logger = structlog.get_logger()

# Module-level refs — set during server lifespan
_imap: ImapMutator | None = None
_decryptor: ProtonDecryptor | None = None

_TEXT_EXTENSIONS = {
    ".txt", ".csv", ".json", ".xml", ".html", ".htm", ".md", ".yaml", ".yml", ".log",
}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _format_date(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _resolve_message(message_id: str):
    """Look up a message row by RFC 2822 Message-ID or pm_id."""
    return db.execute(
        "SELECT * FROM messages WHERE message_id = ? OR pm_id = ?",
        [message_id, message_id],
    ).fetchone()


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

    row = _resolve_message(message_id)
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
    """List attachments on an email.

    Args:
        message_id: The Message-ID header value
        folder: Optional folder hint (unused)
    """
    logger.info("tool.list_attachments", message_id=message_id)

    row = _resolve_message(message_id)
    if row is None:
        return []

    from email_mcp.db import _row_to_message
    msg = _row_to_message(row)

    if not msg.has_attachments:
        return []

    attachments = db.attachments.list_for_message(msg.pm_id)

    if not attachments and not msg.body_indexed:
        return [{"note": "Attachment metadata not yet indexed. Body indexing is still in progress."}]

    logger.info("tool.list_attachments.done", count=len(attachments))
    return [
        {"filename": a["filename"], "size": a["size"], "mime_type": a["mime_type"]}
        for a in attachments
    ]


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "Download Attachment"}
)
async def download_attachment(
    message_id: str,
    filename: str,
    folder: str | None = None,
) -> list[str | Image]:
    """Download and return an email attachment.

    Content is processed server-side:
    - PDFs: extracted to markdown text
    - Images: returned as viewable images
    - Text files: returned as text
    - Other formats: returned as base64-encoded data

    Args:
        message_id: The Message-ID header value
        filename: The attachment filename to download
        folder: Optional folder hint (unused)
    """
    logger.info("tool.download_attachment", message_id=message_id, filename=filename)

    row = _resolve_message(message_id)
    if row is None:
        return [f"Message not found: {message_id}"]

    from email_mcp.db import _row_to_message
    msg = _row_to_message(row)

    att = db.attachments.get(msg.pm_id, filename)
    if att is None:
        return [f"Attachment '{filename}' not found. Run list_attachments first to confirm the filename."]

    # Try API decryptor first (no Bridge needed), fall back to IMAP
    content: bytes | None = None
    if _decryptor and att.get("att_id"):
        try:
            content = await _decryptor.fetch_attachment(
                att["att_id"], att.get("key_packets", "")
            )
        except Exception as e:
            logger.debug("tool.download_attachment.api_failed", error=str(e))

    if content is None and _imap is not None and att.get("part_num"):
        try:
            content = await _imap.fetch_attachment(
                msg.message_id, att["part_num"], folder=msg.folder
            )
        except Exception as e:
            logger.debug("tool.download_attachment.imap_failed", error=str(e))

    if content is None:
        return ["Attachment download unavailable — neither API nor IMAP could fetch it."]

    size = len(content)
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    output: list[str | Image]

    if suffix == ".pdf":
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(content)
            tmp_path = Path(f.name)
        try:
            import pymupdf
            doc = pymupdf.open(str(tmp_path))
            pages = [f"## Page {i+1}\n\n{page.get_text('text').strip()}"
                     for i, page in enumerate(doc) if page.get_text("text").strip()]
            doc.close()
            output = [f"# {filename} ({size} bytes)\n\n" + "\n\n".join(pages)]
        except ImportError:
            output = [f"# {filename} ({size} bytes)\n\n(pymupdf not installed — cannot extract PDF text)"]
        finally:
            tmp_path.unlink(missing_ok=True)

    elif suffix in _IMAGE_EXTENSIONS:
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(content)
            tmp_path = Path(f.name)
        output = [f"Image: {filename} ({size} bytes)", Image(path=tmp_path)]

    elif suffix in _TEXT_EXTENSIONS:
        output = [f"# {filename} ({size} bytes)\n\n{content.decode(errors='replace')}"]

    else:
        encoded = base64.b64encode(content).decode()
        mime_type = att["mime_type"] or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        output = [
            f"Binary file: {filename} ({size} bytes, {mime_type})\n"
            f"Base64-encoded content:\n{encoded}"
        ]

    logger.info("tool.download_attachment.done", filename=filename, size=size)
    return output
