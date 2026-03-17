"""Search tool using SQLite FTS5 with Gmail-style query translation."""

from datetime import datetime, timezone
from typing import Any

import structlog

from email_mcp.db import _row_to_message
from email_mcp.query_builder import build_query
from email_mcp.server import db, mcp

logger = structlog.get_logger()


def _format_date(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "Search Email"}
)
async def search(query: str, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    """Search emails using full-text search and Gmail-style filters.

    Supported operators:
    - from:alice, subject:invoice   (field filters)
    - is:unread, is:read            (read state)
    - in:inbox, in:sent, in:archive (folder)
    - has:attachment                (has attachments)
    - older_than:30d, newer_than:7d (date range, units: h/d/w/m/y)
    - free text                     (full-text search over body + subject)

    Multiple operators are ANDed together.

    Returns results with Message-IDs, which can be used with read_email().

    Args:
        query: Search query using Gmail-style syntax
        limit: Maximum number of results to return
        offset: Number of results to skip
    """
    logger.info("tool.search", query=query, limit=limit, offset=offset)

    parsed = build_query(query)
    sql, params = parsed.to_sql(limit=limit, offset=offset)
    rows = db.execute(sql, params).fetchall()

    results = [_row_to_message(r) for r in rows]
    logger.info("tool.search.done", query=query, count=len(results))

    return [
        {
            "message_id": r.message_id,
            "pm_id": r.pm_id,
            "from": f"{r.sender_name} <{r.sender_email}>" if r.sender_name else r.sender_email,
            "subject": r.subject,
            "date": _format_date(r.date),
            "folder": r.folder,
            "unread": r.unread,
            "has_attachments": r.has_attachments,
        }
        for r in results
    ]
