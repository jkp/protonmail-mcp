"""Search tool using notmuch with IMAP UID resolution via himalaya."""

import asyncio
import re
from typing import Any

import structlog

from protonmail_mcp.server import himalaya, mcp, notmuch

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


def _build_himalaya_query(subject: str, authors: str) -> str:
    """Build a himalaya envelope search query from notmuch metadata."""
    parts = []
    if subject:
        # Use first few significant words to avoid query parse issues
        words = subject.split()[:5]
        safe_subject = " ".join(w for w in words if not w.startswith("-"))
        if safe_subject:
            parts.append(f'subject {safe_subject}')
    if authors:
        # Extract email address from "Name <email>" format
        match = re.search(r"<([^>]+)>", authors)
        addr = match.group(1) if match else authors.split()[0]
        parts.append(f"from {addr}")
    return " and ".join(parts) if parts else ""


async def _resolve_uid(folder: str, subject: str, authors: str) -> str | None:
    """Resolve the correct IMAP UID by searching himalaya for the message."""
    query = _build_himalaya_query(subject, authors)
    if not query:
        return None
    try:
        envelopes = await himalaya.run_json(
            "envelope", "list", "--folder", folder, "--page-size", "1", query
        )
        if envelopes and len(envelopes) > 0:
            return str(envelopes[0]["id"])
    except Exception:
        logger.debug("search.uid_resolve_failed", folder=folder, query=query)
    return None


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
        logger.info(
            "tool.search", query=query, translated=translated, limit=limit, offset=offset
        )
    else:
        logger.info("tool.search", query=query, limit=limit, offset=offset)

    results = await notmuch.search(translated, limit=limit, offset=offset)

    # Resolve correct IMAP UIDs in parallel via himalaya
    async def resolve(r):  # type: ignore[no-untyped-def]
        uid = await _resolve_uid(r.folder, r.subject, r.authors)
        return {
            "uid": uid or r.uid,
            "folder": r.folder,
            "subject": r.subject,
            "date": r.date,
            "authors": r.authors,
        }

    resolved = await asyncio.gather(*[resolve(r) for r in results])
    logger.info("tool.search.done", query=translated, count=len(resolved))
    return list(resolved)
