"""Read email and download attachment tools."""

import base64
import mimetypes
from datetime import UTC, datetime
from typing import Any

import structlog
from fastmcp.utilities.types import Image

from email_mcp.db import resolve_message
from email_mcp.decryptor import ProtonDecryptor
from email_mcp.server import db, mcp
from email_mcp.tools.listing import _web_url

logger = structlog.get_logger()

# Module-level ref — set during server lifespan
_decryptor: ProtonDecryptor | None = None

_TEXT_EXTENSIONS = {
    ".txt",
    ".csv",
    ".json",
    ".xml",
    ".html",
    ".htm",
    ".md",
    ".yaml",
    ".yml",
    ".log",
}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _format_date(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "title": "Read Email"})
async def read_email(
    id: int | str | None = None,
    message_id: str | None = None,
    folder: str | None = None,
) -> dict[str, Any]:
    """Read a full email message by id.

    Args:
        id: Numeric message id (from search or list results)
        message_id: Legacy message_id or pm_id string (use id instead)
        folder: Optional folder hint (unused)
    """
    id = id or message_id
    if id is None:
        return {"error": "missing_id", "detail": "Provide id or message_id."}
    logger.info("tool.read_email", id=id)

    msg = resolve_message(db, id)
    if msg is None:
        return {
            "error": "not_found",
            "id": id,
            "detail": "Message not found in local database. May not have been synced yet.",
        }

    from email_mcp.convert import body_for_display

    body = db.bodies.get(msg.pm_id) or ""

    # Convert HTML to markdown for efficient LLM consumption
    body = body_for_display(body)

    logger.info(
        "tool.read_email.done",
        id=msg.row_id,
        subject=msg.subject,
        body_len=len(body),
        body_indexed=msg.body_indexed,
    )

    return {
        "id": msg.row_id,
        "from": (
            f"{msg.sender_name} <{msg.sender_email}>" if msg.sender_name else msg.sender_email
        ),
        "to": msg.recipients,
        "subject": msg.subject,
        "date": _format_date(msg.date),
        "body": body,
        "folder": msg.folder,
        "unread": msg.unread,
        "has_attachments": msg.has_attachments,
        "body_indexed": msg.body_indexed,
        "web_url": _web_url(msg.conversation_id, msg.folder),
    }


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "title": "List Attachments"})
async def list_attachments(
    id: int | str | None = None,
    message_id: str | None = None,
    folder: str | None = None,
) -> list[dict[str, Any]]:
    """List attachments on an email.

    Args:
        id: Numeric message id (from search or list results)
        message_id: Legacy message_id or pm_id string (use id instead)
        folder: Optional folder hint (unused)
    """
    id = id or message_id
    if id is None:
        return [{"error": "missing_id", "detail": "Provide id or message_id."}]
    logger.info("tool.list_attachments", id=id)

    msg = resolve_message(db, id)
    if msg is None:
        return []

    if not msg.has_attachments:
        return []

    attachments = db.attachments.list_for_message(msg.pm_id)

    if not attachments and not msg.body_indexed:
        return [
            {"note": "Attachment metadata not yet indexed. Body indexing is still in progress."}
        ]

    logger.info("tool.list_attachments.done", count=len(attachments))
    return [
        {"filename": a["filename"], "size": a["size"], "mime_type": a["mime_type"]}
        for a in attachments
    ]


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "Download Attachment"}
)
async def download_attachment(
    id: int | str | None = None,
    message_id: str | None = None,
    filename: str = "",
    folder: str | None = None,
) -> list[str | Image]:
    """Download and return an email attachment.

    Content is processed server-side:
    - PDFs: extracted to markdown text
    - Images: returned as viewable images
    - Text files: returned as text
    - Other formats: returned as base64-encoded data

    Args:
        id: Numeric message id (from search or list results)
        message_id: Legacy message_id or pm_id string (use id instead)
        filename: The attachment filename to download
        folder: Optional folder hint (unused)
    """
    id = id or message_id
    if id is None:
        return ["Error: missing id. Provide id or message_id."]
    logger.info("tool.download_attachment", id=id, filename=filename)

    msg = resolve_message(db, id)
    if msg is None:
        return [f"Message not found: {id}"]

    att = db.attachments.get(msg.pm_id, filename)
    if att is None:
        return [
            f"Attachment '{filename}' not found."
            " Run list_attachments first to confirm the filename."
        ]

    if _decryptor is None:
        return ["Decryptor not initialized — attachment download unavailable."]

    if not att.get("att_id"):
        return ["Attachment metadata missing att_id — message may need re-indexing."]

    try:
        content = await _decryptor.fetch_attachment(att["att_id"], att.get("key_packets", ""))
    except Exception as e:
        return [f"Failed to fetch attachment: {e}"]

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
            pages = [
                f"## Page {i + 1}\n\n{page.get_text('text').strip()}"
                for i, page in enumerate(doc)  # type: ignore[arg-type]
                if page.get_text("text").strip()
            ]
            doc.close()
            output = [f"# {filename} ({size} bytes)\n\n" + "\n\n".join(pages)]
        except ImportError:
            output = [
                f"# {filename} ({size} bytes)\n\n(pymupdf not installed — cannot extract PDF text)"
            ]
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
        mime_type = (
            att["mime_type"] or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )
        output = [
            f"Binary file: {filename} ({size} bytes, {mime_type})\n"
            f"Base64-encoded content:\n{encoded}"
        ]

    logger.info("tool.download_attachment.done", filename=filename, size=size)
    return output
