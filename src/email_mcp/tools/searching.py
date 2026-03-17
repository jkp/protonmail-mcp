"""Search tool using notmuch with Gmail-style query translation."""

from typing import Any

import structlog

from email_mcp.search import NotmuchSearcher, translate_query
from email_mcp.server import mcp, settings

logger = structlog.get_logger()

_notmuch_config = settings.maildir_path / ".notmuch" / "config"
_searcher = NotmuchSearcher(
    bin_path=settings.notmuch_bin,
    config_path=str(_notmuch_config),
    maildir_root=str(settings.maildir_path),
)


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "Search Email"}
)
async def search(query: str, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    """Search emails using full-text search.

    Supports both notmuch and Gmail-style search syntax:
    - from:alice, to:bob, subject:report (native notmuch)
    - has:attachment, is:unread, is:starred (Gmail-style, auto-translated)
    - in:inbox, in:sent (Gmail-style folder search)
    - label:important (translated to tag:important)
    - filename:report.pdf (translated to attachment:report.pdf)
    - newer_than:7d, older_than:30d (date range shortcuts)

    Returns results with Message-IDs, which can be used with read_email().

    Args:
        query: Search query (Gmail-style or notmuch syntax)
        limit: Maximum number of results to return
        offset: Number of results to skip
    """
    translated = translate_query(query)
    if translated != query:
        logger.info("tool.search", query=query, translated=translated, limit=limit, offset=offset)
    else:
        logger.info("tool.search", query=query, limit=limit, offset=offset)

    results = await _searcher.search(translated, limit=limit, offset=offset)
    logger.info("tool.search.done", query=translated, count=len(results))

    return [
        {
            "message_id": r.message_id,
            "folders": r.folders,
            "subject": r.subject,
            "date": r.date,
            "authors": r.authors,
            "tags": list(r.tags),
        }
        for r in results
    ]
