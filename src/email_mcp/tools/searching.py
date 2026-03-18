"""Search tool using FTS5 + semantic vector search."""

from datetime import datetime, timezone
from typing import Any

import structlog

from email_mcp.db import _row_to_message
from email_mcp.embedder import Embedder
from email_mcp.query_builder import build_query
from email_mcp.server import db, mcp

logger = structlog.get_logger()

# Module-level ref — set during server lifespan
_embedder: Embedder | None = None


def _format_date(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def _format_result(r) -> dict[str, Any]:
    return {
        "message_id": r.message_id,
        "pm_id": r.pm_id,
        "from": (
            f"{r.sender_name} <{r.sender_email}>"
            if r.sender_name
            else r.sender_email
        ),
        "subject": r.subject,
        "date": _format_date(r.date),
        "folder": r.folder,
        "unread": r.unread,
        "has_attachments": r.has_attachments,
    }


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "title": "Search Email",
    }
)
async def search(
    query: str, limit: int = 20, offset: int = 0
) -> list[dict[str, Any]]:
    """Search emails using semantic search with optional precise filters.

    Semantic queries find emails by meaning, not exact keywords:
    - "benson headphones" — finds emails mentioning Benson even if
      the sender address is es.lab.audio.hk@gmail.com
    - "house renovation quotes" — finds relevant emails regardless
      of exact wording

    Combine with precise filters for time, folder, and state:
    - "benson headphones newer_than:4w"
    - "project updates in:inbox is:unread"
    - "invoice from accountant newer_than:30d has:attachment"

    Available filters:
    - from:, to:, subject:    (field match)
    - is:unread, is:read      (read state)
    - in:inbox, in:sent, etc. (folder)
    - has:attachment           (attachments)
    - newer_than:, older_than: (time range: h/d/w/m/y)
    - filename:               (attachment name)

    Filters are applied as SQL constraints BEFORE semantic ranking.
    Free text is matched semantically against email content, sender
    names, and subjects.

    Args:
        query: Search query — natural language and/or Gmail-style filters
        limit: Maximum number of results to return
        offset: Number of results to skip
    """
    logger.info("tool.search", query=query, limit=limit, offset=offset)

    parsed = build_query(query)

    # Phase 1: FTS5 + metadata search
    sql, params = parsed.to_sql(limit=limit, offset=offset)
    try:
        rows = db.execute(sql, params).fetchall()
    except Exception as e:
        logger.warning("tool.search.fts_error", error=str(e))
        rows = []

    results = [_row_to_message(r) for r in rows]
    seen_pm_ids = {r.pm_id for r in results}

    # Phase 2: Vector search if we have an embedder and free-text terms
    if _embedder and parsed.fts_terms and len(results) < limit:
        remaining = limit - len(results)
        try:
            if parsed.where_clauses:
                vector_pm_ids = _embedder.search_with_filters(
                    parsed.fts_terms,
                    where_clause=parsed.where,
                    params=parsed.params,
                    limit=remaining + len(seen_pm_ids),
                )
            else:
                vector_pm_ids = _embedder.search(
                    parsed.fts_terms,
                    limit=remaining + len(seen_pm_ids),
                )

            # Add vector results not already in FTS results
            for pm_id in vector_pm_ids:
                if pm_id in seen_pm_ids:
                    continue
                msg = db.messages.get(pm_id)
                if msg:
                    results.append(msg)
                    seen_pm_ids.add(pm_id)
                if len(results) >= limit:
                    break

            logger.info(
                "tool.search.vector",
                query=parsed.fts_terms,
                vector_hits=len(vector_pm_ids),
                added=len(results) - len(rows),
            )
        except Exception as e:
            logger.warning("tool.search.vector_error", error=str(e))

    logger.info("tool.search.done", query=query, count=len(results))
    return [_format_result(r) for r in results]
