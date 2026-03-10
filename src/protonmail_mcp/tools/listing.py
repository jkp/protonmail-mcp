"""List emails and folders tools."""

from typing import Any

from protonmail_mcp.models import Envelope, Folder
from protonmail_mcp.server import himalaya, mcp


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "List Emails"}
)
async def list_emails(
    folder: str = "INBOX",
    page: int = 1,
    page_size: int = 20,
) -> list[dict[str, Any]]:
    """List email envelopes from a folder.

    Args:
        folder: Mail folder to list (default: INBOX)
        page: Page number (1-indexed)
        page_size: Number of emails per page
    """
    data = await himalaya.run_json(
        "envelope",
        "list",
        "--folder",
        folder,
        "--page",
        str(page),
        "--page-size",
        str(page_size),
    )
    envelopes = [Envelope.model_validate(item) for item in data]
    return [
        {
            "id": e.id,
            "from": str(e.from_),
            "to": [str(addr) for addr in e.to],
            "subject": e.subject,
            "date": e.date,
            "has_attachment": e.has_attachment,
        }
        for e in envelopes
    ]


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "List Folders"}
)
async def list_folders() -> list[dict[str, Any]]:
    """List all mail folders."""
    data = await himalaya.run_json("folder", "list")
    folders = [Folder.model_validate(item) for item in data]
    return [{"name": f.name, "desc": f.desc} for f in folders]
