"""List emails and folders tools."""

from typing import Any

import structlog

from email_mcp.server import db, mcp

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
    Use search() when you need full-text or filtered queries.

    Returns a dict with total count and pagination info so you know if more
    pages are available.

    Args:
        folder: Mail folder to list (default: INBOX)
        limit: Maximum number of emails to return
        offset: Number of emails to skip
    """
    logger.info("tool.list_emails", folder=folder, limit=limit, offset=offset)

    rows = db.messages.list_by_folder(folder=folder, limit=limit, offset=offset)
    counts = db.messages.count_by_folder(folder=folder)
    total = counts["total"]

    logger.info("tool.list_emails.done", count=len(rows), total=total)
    return {
        "total": total,
        "offset": offset,
        "count": len(rows),
        "emails": [
            {
                "message_id": r.message_id,
                "pm_id": r.pm_id,
                "from": f"{r.sender_name} <{r.sender_email}>" if r.sender_name else r.sender_email,
                "to": r.recipients,
                "subject": r.subject,
                "date": r.date,
                "folder": r.folder,
                "unread": bool(r.unread),
                "has_attachments": bool(r.has_attachments),
            }
            for r in rows
        ],
    }


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "List Folders"}
)
async def list_folders() -> list[dict[str, Any]]:
    """List all mail folders with message counts."""
    logger.info("tool.list_folders")

    rows = db.execute(
        """
        SELECT folder, COUNT(*) as total, SUM(unread) as unread_count
        FROM messages
        WHERE folder IS NOT NULL
        GROUP BY folder
        ORDER BY folder
        """
    ).fetchall()

    logger.info("tool.list_folders.done", count=len(rows))
    return [
        {
            "name": row[0],
            "count": row[1],
            "unread": row[2] or 0,
        }
        for row in rows
    ]
