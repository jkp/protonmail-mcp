"""Read email and download attachment tools."""

import mimetypes
import tempfile
from pathlib import Path
from typing import Any

import structlog
from fastmcp.utilities.types import Image

from protonmail_mcp.convert import html_to_markdown
from protonmail_mcp.server import himalaya, mcp
from protonmail_mcp.template import parse_template

logger = structlog.get_logger()

_TEXT_EXTENSIONS = {
    ".txt", ".csv", ".json", ".xml", ".html", ".htm", ".md", ".yaml", ".yml", ".log",
}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "Read Email"}
)
async def read_email(email_id: str, folder: str = "INBOX") -> dict[str, Any]:
    """Read a full email message.

    Args:
        email_id: The email ID/UID to read
        folder: The folder containing the email
    """
    logger.info("tool.read_email", email_id=email_id, folder=folder)
    # himalaya message read returns a JSON string (template format), not a structured object
    raw = await himalaya.run_json("message", "read", email_id, "--folder", folder)
    parsed = parse_template(raw)

    # Prefer HTML converted to markdown, fall back to plain text
    if parsed["text/html"]:
        body = html_to_markdown(parsed["text/html"])
    else:
        body = parsed["text/plain"]

    logger.info(
        "tool.read_email.done",
        email_id=email_id,
        subject=parsed["subject"],
        body_len=len(body),
    )
    return {
        "id": email_id,
        "from": parsed["from"],
        "to": parsed["to"],
        "cc": parsed["cc"],
        "subject": parsed["subject"],
        "date": parsed["date"],
        "body": body,
    }


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "title": "List Attachments",
    }
)
async def list_attachments(email_id: str, folder: str = "INBOX") -> list[dict[str, Any]]:
    """List attachments on an email without downloading them.

    Use the returned filenames with download_attachment to fetch content.

    Args:
        email_id: The email ID/UID
        folder: The folder containing the email
    """
    logger.info("tool.list_attachments", email_id=email_id, folder=folder)
    tmpdir = tempfile.mkdtemp()
    await himalaya.run(
        "attachment",
        "download",
        email_id,
        "--folder",
        folder,
        "--dir",
        tmpdir,
    )

    downloaded = list(Path(tmpdir).glob("*"))
    attachments = []
    for f in downloaded:
        mime_type, _ = mimetypes.guess_type(f.name)
        attachments.append({
            "filename": f.name,
            "size": f.stat().st_size,
            "mime_type": mime_type or "application/octet-stream",
        })

    logger.info("tool.list_attachments.done", email_id=email_id, count=len(attachments))
    return attachments


async def _download_to_tmpdir(email_id: str, folder: str, filename: str) -> Path:
    """Download attachments and return path to the requested file."""
    tmpdir = tempfile.mkdtemp()
    await himalaya.run(
        "attachment",
        "download",
        email_id,
        "--folder",
        folder,
        "--dir",
        tmpdir,
    )
    target = Path(tmpdir) / filename
    if not target.exists():
        raise FileNotFoundError(f"Attachment '{filename}' not found in email {email_id}")
    return target


def _extract_pdf_text(path: Path) -> str:
    """Extract text from a PDF file."""
    import pymupdf

    doc = pymupdf.open(str(path))
    pages = []
    for i, page in enumerate(doc):
        text = page.get_text("text").strip()
        if text:
            pages.append(f"## Page {i + 1}\n\n{text}")
    doc.close()
    return "\n\n".join(pages)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "title": "Download Attachment",
    }
)
async def download_attachment(
    email_id: str,
    folder: str,
    filename: str,
) -> list[str | Image]:
    """Download and return an email attachment.

    Content is processed server-side for best results:
    - PDFs: extracted to markdown text
    - Images: returned as viewable images
    - Text files (CSV, JSON, XML, etc.): returned as text
    - Other formats: returned as base64-encoded data

    Use list_attachments first to discover available filenames.

    Args:
        email_id: The email ID/UID
        folder: The folder containing the email
        filename: The attachment filename to download
    """
    logger.info("tool.download_attachment", email_id=email_id, folder=folder, filename=filename)
    target = await _download_to_tmpdir(email_id, folder, filename)
    suffix = target.suffix.lower()
    size = target.stat().st_size

    result: list[str | Image]

    if suffix == ".pdf":
        text = _extract_pdf_text(target)
        logger.info(
            "tool.download_attachment.done",
            email_id=email_id, filename=filename, type="pdf", chars=len(text),
        )
        result = [f"# {filename} ({size} bytes)\n\n{text}"]

    elif suffix in _IMAGE_EXTENSIONS:
        logger.info(
            "tool.download_attachment.done",
            email_id=email_id, filename=filename, type="image", size=size,
        )
        result = [f"Image: {filename} ({size} bytes)", Image(path=target)]

    elif suffix in _TEXT_EXTENSIONS:
        text = target.read_text(errors="replace")
        logger.info(
            "tool.download_attachment.done",
            email_id=email_id, filename=filename, type="text", chars=len(text),
        )
        result = [f"# {filename} ({size} bytes)\n\n{text}"]

    else:
        import base64

        encoded = base64.b64encode(target.read_bytes()).decode()
        mime_type, _ = mimetypes.guess_type(filename)
        logger.info(
            "tool.download_attachment.done",
            email_id=email_id, filename=filename, type="binary", size=size,
        )
        result = [
            f"Binary file: {filename} ({size} bytes, {mime_type or 'unknown type'})\n"
            f"Base64-encoded content:\n{encoded}"
        ]

    return result
