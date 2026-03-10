"""Read email and download attachment tools."""

import base64
import tempfile
from pathlib import Path
from typing import Any

import structlog

from protonmail_mcp.convert import html_to_markdown
from protonmail_mcp.server import himalaya, mcp
from protonmail_mcp.template import parse_template

logger = structlog.get_logger()

_LARGE_ATTACHMENT_THRESHOLD = 10 * 1024 * 1024  # 10MB


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
        "title": "Download Attachment",
    }
)
async def download_attachment(email_id: str, folder: str, filename: str) -> dict[str, Any]:
    """Download an email attachment.

    Args:
        email_id: The email ID/UID
        folder: The folder containing the email
        filename: The attachment filename to download
    """
    logger.info("tool.download_attachment", email_id=email_id, folder=folder, filename=filename)
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

    # Find the downloaded file
    downloaded = list(Path(tmpdir).glob("*"))
    target = None
    for f in downloaded:
        if f.name == filename:
            target = f
            break

    if target is None:
        logger.warning("tool.download_attachment.not_found", email_id=email_id, filename=filename)
        return {"error": f"Attachment '{filename}' not found"}

    size = target.stat().st_size
    result: dict[str, Any] = {
        "filename": target.name,
        "size": size,
        "path": str(target),
    }

    if size <= _LARGE_ATTACHMENT_THRESHOLD:
        content = target.read_bytes()
        result["content_base64"] = base64.b64encode(content).decode()

    logger.info("tool.download_attachment.done", email_id=email_id, filename=filename, size=size)
    return result
