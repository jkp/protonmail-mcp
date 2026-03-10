"""Search tool using notmuch."""

import re
from typing import Any

import structlog

from protonmail_mcp.server import mcp, notmuch

logger = structlog.get_logger()

# Gmail-style query translations to notmuch syntax
_QUERY_TRANSLATIONS = [
    (re.compile(r"\bhas:attachment\b"), "tag:attachment"),
    (re.compile(r"\bis:unread\b"), "tag:unread"),
    (re.compile(r"\bis:read\b"), "not tag:unread"),
    (re.compile(r"\bis:starred\b"), "tag:flagged"),
    (re.compile(r"\bis:flagged\b"), "tag:flagged"),
    (re.compile(r"\bin:(\S+)"), r"folder:\1"),
    (re.compile(r"\blabel:(\S+)"), r"tag:\1"),
    (re.compile(r"\bfilename:(\S+)"), r"attachment:\1"),
    (re.compile(r"\bnewer_than:(\d+)d\b"), r"date:\1days.."),
    (re.compile(r"\bolder_than:(\d+)d\b"), r"date:..\1days"),
]


def _translate_query(query: str) -> str:
    """Translate Gmail-style search operators to notmuch syntax."""
    translated = query
    for pattern, replacement in _QUERY_TRANSLATIONS:
        translated = pattern.sub(replacement, translated)
    return translated


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

    Returns results with IMAP UIDs and folder names, which can be used
    directly with read_email(email_id=uid, folder=folder).

    Args:
        query: Search query (Gmail-style or notmuch syntax)
        limit: Maximum number of results to return
        offset: Number of results to skip
    """
    translated = _translate_query(query)
    if translated != query:
        logger.info("tool.search", query=query, translated=translated, limit=limit, offset=offset)
    else:
        logger.info("tool.search", query=query, limit=limit, offset=offset)
    results = await notmuch.search(translated, limit=limit, offset=offset)
    logger.info("tool.search.done", query=translated, count=len(results))
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
