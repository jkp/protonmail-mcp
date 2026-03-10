"""Search tool using notmuch."""

from typing import Any

from protonmail_mcp.server import mcp, notmuch


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "Search Email"}
)
async def search(query: str, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    """Search emails using notmuch full-text search.

    Returns results with IMAP UIDs and folder names, which can be used
    directly with read_email(email_id=uid, folder=folder).

    Args:
        query: Notmuch search query (e.g., 'from:alice', 'tag:inbox', 'subject:report')
        limit: Maximum number of results to return
        offset: Number of results to skip
    """
    results = await notmuch.search(query, limit=limit, offset=offset)
    return [
        {
            "uid": r.uid,
            "folder": r.folder,
            "subject": r.subject,
            "date": r.date,
            "authors": r.authors,
        }
        for r in results
    ]
