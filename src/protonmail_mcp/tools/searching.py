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


_STOP_WORDS = {"re:", "fw:", "fwd:", "the", "a", "an", "and", "or", "to", "for", "in", "on", "at", "is"}


def _extract_from_addr(authors: str) -> str | None:
    """Extract email address from 'Name <email>' or plain email format."""
    if not authors:
        return None
    match = re.search(r"<([^>]+)>", authors)
    return match.group(1) if match else authors.split()[0]


def _pick_subject_keyword(subject: str) -> str | None:
    """Pick the most distinctive word from a subject for IMAP SEARCH.

    Protonmail Bridge only supports unquoted single-word subject searches.
    Strips common prefixes (Re:, Fwd:, [bracketed]), then picks the longest
    word from the remaining subject to maximize distinctiveness.
    """
    if not subject:
        return None
    # Strip Re:/Fwd: prefixes and [bracketed] groups (e.g. [org/repo])
    cleaned = re.sub(r"^(Re|Fwd|Fw):\s*", "", subject, flags=re.IGNORECASE)
    cleaned = re.sub(r"\[[^\]]*\]", "", cleaned)
    words = re.findall(r"[a-zA-Z0-9]+", cleaned)
    candidates = [w for w in words if w.lower() not in _STOP_WORDS and len(w) > 2]
    return max(candidates, key=len) if candidates else None


async def _resolve_uid(folder: str, subject: str, authors: str) -> str | None:
    """Resolve the correct IMAP UID by searching himalaya for the message.

    Uses from-address + a single subject keyword for disambiguation.
    Protonmail Bridge's IMAP SEARCH doesn't support multi-word quoted
    subject queries, but single unquoted keywords work.
    """
    addr = _extract_from_addr(authors)
    if not addr:
        return None
    query = f"from {addr}"
    keyword = _pick_subject_keyword(subject)
    if keyword:
        query += f" and subject {keyword}"
    try:
        envelopes = await himalaya.run_json(
            "envelope", "list", "--folder", folder, "--page-size", "5", query
        )
        if not envelopes:
            return None
        # Prefer exact subject match
        for env in envelopes:
            if env.get("subject") == subject:
                return str(env["id"])
        # Fall back to first result
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
