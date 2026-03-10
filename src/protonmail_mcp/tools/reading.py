"""Read email and download attachment tools."""

import mimetypes
import tempfile
from pathlib import Path
from typing import Any

import structlog
from fastmcp.resources import ResourceContent, ResourceResult

from protonmail_mcp.convert import html_to_markdown
from protonmail_mcp.server import himalaya, mcp
from protonmail_mcp.template import parse_template

logger = structlog.get_logger()


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

    Use the returned filenames with the attachment resource:
    attachment://{folder}/{email_id}/{filename}

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


@mcp.resource("attachment://{folder}/{email_id}/{filename}")
async def attachment_resource(folder: str, email_id: str, filename: str) -> ResourceResult:
    """Fetch an email attachment as a binary resource.

    Use list_attachments tool first to discover available filenames.
    """
    logger.info("resource.attachment", email_id=email_id, folder=folder, filename=filename)
    target = await _download_to_tmpdir(email_id, folder, filename)
    data = target.read_bytes()
    mime_type, _ = mimetypes.guess_type(filename)
    mime_type = mime_type or "application/octet-stream"
    logger.info(
        "resource.attachment.done",
        email_id=email_id,
        filename=filename,
        size=len(data),
        mime_type=mime_type,
    )
    return ResourceResult(
        contents=[ResourceContent(content=data, mime_type=mime_type)]
    )
