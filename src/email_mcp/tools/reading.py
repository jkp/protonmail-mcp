"""Read email and download attachment tools."""

import base64
import mimetypes
from typing import Any

import structlog
from fastmcp.utilities.types import Image

from email_mcp.models import Email
from email_mcp.server import mcp, store
from email_mcp.tools.searching import _searcher

logger = structlog.get_logger()

_TEXT_EXTENSIONS = {
    ".txt", ".csv", ".json", ".xml", ".html", ".htm", ".md", ".yaml", ".yml", ".log",
}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


async def _resolve_email(message_id: str, folder: str | None = None) -> Email | None:
    """Resolve a message_id to an Email, using notmuch for fast path lookup."""
    # Fast path: use notmuch to find the file
    path = await _searcher.find_message_path(message_id)
    if path:
        email = store.read_email_by_path(path, message_id)
        if email is not None:
            return email

    # Slow fallback: scan Maildir by Message-ID header
    return store.read_email(message_id, folder=folder)


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "Read Email"}
)
async def read_email(message_id: str, folder: str | None = None) -> dict[str, Any]:
    """Read a full email message by Message-ID.

    Args:
        message_id: The Message-ID header value (from search or list results)
        folder: Optional folder hint to speed up lookup
    """
    logger.info("tool.read_email", message_id=message_id, folder=folder)
    email = await _resolve_email(message_id, folder)
    if email is None:
        return {"error": f"Email not found: {message_id}"}

    body = email.body_html if email.body_html else email.body_plain

    logger.info(
        "tool.read_email.done",
        message_id=message_id,
        subject=email.subject,
        body_len=len(body),
    )
    return {
        "message_id": email.message_id,
        "from": str(email.from_),
        "to": [str(addr) for addr in email.to],
        "cc": [str(addr) for addr in email.cc],
        "subject": email.subject,
        "date": email.date_str,
        "body": body,
        "folder": email.folder,
        "flags": email.flags,
        "attachments": [
            {"filename": a.filename, "content_type": a.content_type, "size": a.size}
            for a in email.attachments
        ],
    }


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "List Attachments"}
)
async def list_attachments(message_id: str, folder: str | None = None) -> list[dict[str, Any]]:
    """List attachments on an email without downloading them.

    Args:
        message_id: The Message-ID header value
        folder: Optional folder hint
    """
    logger.info("tool.list_attachments", message_id=message_id, folder=folder)
    email = await _resolve_email(message_id, folder)
    if email is None:
        return []

    logger.info("tool.list_attachments.done", count=len(email.attachments))
    return [
        {
            "filename": a.filename,
            "content_type": a.content_type,
            "size": a.size,
        }
        for a in email.attachments
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
        folder: Optional folder hint
    """
    logger.info("tool.download_attachment", message_id=message_id, filename=filename)

    # Use notmuch to find file path, then get attachment from store
    path = await _searcher.find_message_path(message_id)
    if path:
        result = store.get_attachment_content_by_path(path, filename)
    else:
        result = store.get_attachment_content(message_id, filename, folder=folder)

    if result is None:
        return [f"Attachment '{filename}' not found"]

    content, content_type = result
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
            pages = []
            for i, page in enumerate(doc):
                text = page.get_text("text").strip()
                if text:
                    pages.append(f"## Page {i + 1}\n\n{text}")
            doc.close()
            text = "\n\n".join(pages)
        except ImportError:
            text = "(pymupdf not installed — cannot extract PDF text)"
        finally:
            tmp_path.unlink(missing_ok=True)
        output = [f"# {filename} ({size} bytes)\n\n{text}"]

    elif suffix in _IMAGE_EXTENSIONS:
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(content)
            tmp_path = Path(f.name)
        output = [f"Image: {filename} ({size} bytes)", Image(path=tmp_path)]

    elif suffix in _TEXT_EXTENSIONS:
        text = content.decode(errors="replace")
        output = [f"# {filename} ({size} bytes)\n\n{text}"]

    else:
        encoded = base64.b64encode(content).decode()
        mime_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        output = [
            f"Binary file: {filename} ({size} bytes, {mime_type})\n"
            f"Base64-encoded content:\n{encoded}"
        ]

    logger.info("tool.download_attachment.done", filename=filename, size=size)
    return output
