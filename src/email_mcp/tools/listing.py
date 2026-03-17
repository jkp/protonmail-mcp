"""List emails and folders tools."""

from typing import Any

import structlog

from email_mcp.server import mcp, store

logger = structlog.get_logger()


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "List Emails"}
)
async def list_emails(
    folder: str = "INBOX",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """List email summaries from a folder, sorted newest first.

    Use this to browse a folder (INBOX, Sent, Archive, etc.) without searching.
    Faster than search — reads directly from Maildir, no indexing needed.
    Use search() only when you need full-text or filtered queries.

    Returns a dict with total count and pagination info so you know if more
    pages are available.

    Args:
        folder: Mail folder to list (default: INBOX)
        limit: Maximum number of emails to return
        offset: Number of emails to skip
    """
    logger.info("tool.list_emails", folder=folder, limit=limit, offset=offset)
    total = store.count_emails(folder=folder)
    emails = store.list_emails(folder=folder, limit=limit, offset=offset)
    logger.info("tool.list_emails.done", count=len(emails), total=total)
    return {
        "total": total,
        "offset": offset,
        "count": len(emails),
        "emails": [
            {
                "message_id": e.message_id,
                "from": str(e.from_),
                "to": [str(addr) for addr in e.to],
                "subject": e.subject,
                "date": e.date_str,
                "folder": e.folder,
                "flags": e.flags,
            }
            for e in emails
        ],
    }


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "List Folders"}
)
async def list_folders() -> list[dict[str, Any]]:
    """List all mail folders with message counts."""
    logger.info("tool.list_folders")
    folders = store.list_folders()
    logger.info("tool.list_folders.done", count=len(folders))
    return [
        {
            "name": f.name,
            "count": f.count,
            "unread": f.unread,
        }
        for f in folders
    ]
