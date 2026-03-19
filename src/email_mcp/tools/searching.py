"""Search tool using FTS5 + semantic vector search."""

from datetime import UTC, datetime
from typing import Any

import structlog

from email_mcp.db import _row_to_message
from email_mcp.embedder import Embedder
from email_mcp.query_builder import build_query
from email_mcp.server import db, mcp
from email_mcp.tools.listing import _web_url

logger = structlog.get_logger()

# Module-level ref — set during server lifespan
_embedder: Embedder | None = None

# ---------------------------------------------------------------------------
# Promotional email discriminator
# ---------------------------------------------------------------------------
# Emails containing these markers in the body are almost certainly
# newsletters / marketing blasts. Downranked unless the query is
# explicitly looking for that kind of content.

_PROMO_MARKERS = [
    "unsubscribe",
    "avregistrera",  # Swedish: unsubscribe / unregister
    "prenumeration",  # Swedish: subscription
]

_PROMO_QUERY_TERMS = frozenset(
    {
        "newsletter",
        "newsletters",
        "promo",
        "promotional",
        "promotions",
        "subscription",
        "subscriptions",
        "unsubscribe",
        "marketing",
        "mailing list",
    }
)


def is_promotional(body: str) -> bool:
    """Return True if the email body contains promotional unsubscribe markers."""
    if not body:
        return False
    body_lower = body.lower()
    return any(marker in body_lower for marker in _PROMO_MARKERS)


def wants_promos(query: str) -> bool:
    """Return True if the query is explicitly searching for promotional content."""
    query_lower = query.lower()
    return any(term in query_lower for term in _PROMO_QUERY_TERMS)


def check_bulk(pm_ids: list[str], db_ref: Any) -> set[str]:
    """Return the subset of pm_ids that are bulk/promotional.

    Uses a layered approach, best signal first:
    1. newsletter_id (from metadata — free)
    2. List-Unsubscribe in parsed_headers (if headers indexed)
    3. Body text scan for unsubscribe markers (fallback when headers not yet indexed)
    """
    if not pm_ids:
        return set()

    bulk: set[str] = set()
    needs_body_check: list[str] = []

    placeholders = ",".join("?" * len(pm_ids))
    rows = db_ref.execute(
        f"SELECT pm_id, newsletter_id, headers_indexed,"
        f" json_extract(parsed_headers, '$.\"List-Unsubscribe\"') as list_unsub"
        f" FROM messages WHERE pm_id IN ({placeholders})",
        list(pm_ids),
    ).fetchall()

    for row in rows:
        pm_id, nl_id, hdrs_indexed, list_unsub = row[0], row[1], row[2], row[3]
        if nl_id:
            bulk.add(pm_id)
        elif hdrs_indexed and list_unsub:
            bulk.add(pm_id)
        elif not hdrs_indexed:
            needs_body_check.append(pm_id)

    for pm_id in needs_body_check:
        body = db_ref.bodies.get(pm_id) or ""
        if is_promotional(body):
            bulk.add(pm_id)

    return bulk


def apply_bulk_penalty(
    query: str,
    scored: list[tuple[float, Any]],
    db_ref: Any,
) -> list[tuple[float, Any]]:
    """Partition scored results: non-bulk first, bulk last.

    When the query wants promos/newsletters, the original order is preserved.
    """
    if wants_promos(query):
        return scored

    candidate_ids = [msg.pm_id for _, msg in scored]
    bulk_ids = check_bulk(candidate_ids, db_ref)

    if not bulk_ids:
        return scored

    non_bulk = [(s, m) for s, m in scored if m.pm_id not in bulk_ids]
    bulk = [(s, m) for s, m in scored if m.pm_id in bulk_ids]

    logger.info(
        "tool.search.bulk_downranked",
        bulk_count=len(bulk),
        non_bulk_count=len(non_bulk),
    )

    return non_bulk + bulk


def _dedup_conversations(scored: list[tuple[float, Any]]) -> list[tuple[float, Any]]:
    """Keep only the best-scoring message per conversation + per (sender, subject).

    Two layers:
    1. conversation_id — collapses thread replies (Self cert, Wiki, etc.)
    2. (sender_email, subject) — collapses repeated templates (appointment confirmations)

    Already sorted by score desc, so first seen wins.
    """
    seen_convos: set[str] = set()
    seen_sender_subj: set[tuple[str, str]] = set()
    result: list[tuple[float, Any]] = []

    for score, msg in scored:
        # Conversation dedup (skip if no conversation_id)
        if msg.conversation_id:
            if msg.conversation_id in seen_convos:
                continue
            seen_convos.add(msg.conversation_id)

        # Sender+subject dedup for repeated templates
        key = (msg.sender_email or "", msg.subject or "")
        if key in seen_sender_subj:
            continue
        seen_sender_subj.add(key)

        result.append((score, msg))

    if len(result) < len(scored):
        logger.info(
            "tool.search.deduped",
            before=len(scored),
            after=len(result),
        )

    return result


def _format_date(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_result(r) -> dict[str, Any]:
    return {
        "id": r.row_id,
        "from": (f"{r.sender_name} <{r.sender_email}>" if r.sender_name else r.sender_email),
        "subject": r.subject,
        "date": _format_date(r.date),
        "folder": r.folder,
        "unread": r.unread,
        "has_attachments": r.has_attachments,
        "web_url": _web_url(r.conversation_id, r.folder),
    }


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "title": "Search Email",
    }
)
async def search(query: str, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
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

    Available filters (Gmail-style syntax):
    - from:, to:, subject:    (field match)
    - is:unread, is:read      (read state)
    - in:inbox, in:sent, etc. (folder)
    - has:attachment           (attachments)
    - newer_than:, older_than: (time range: h/d/w/m/y)
    - filename:               (attachment name)

    IMPORTANT: Multi-word filter values MUST be quoted. This follows
    Gmail syntax — unquoted values stop at the first space:
    - from:"Companies House"      ← correct (matches full name)
    - from:Companies House        ← WRONG (searches from:Companies + freetext "House")
    - subject:"meeting notes"     ← correct
    - subject:meeting notes       ← WRONG (searches subject:meeting + freetext "notes")

    Single-word values don't need quotes:
    - from:ferdi                  ← fine
    - subject:invoice             ← fine

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

    results: list = []

    if parsed.raw_fts_terms:
        soft_candidates: list = []
        seen_pm_ids: set[str] = set()

        # Phase 1: Vector search
        if _embedder:
            try:
                if parsed.where_clauses:
                    vector_pm_ids = _embedder.search_with_filters(
                        parsed.raw_fts_terms,
                        where_clause=parsed.where,
                        params=parsed.params,
                        limit=limit,
                    )
                else:
                    vector_pm_ids = _embedder.search(parsed.raw_fts_terms, limit=limit)

                for pm_id in vector_pm_ids:
                    msg = db.messages.get(pm_id)
                    if msg:
                        soft_candidates.append(msg)
                        seen_pm_ids.add(pm_id)

                logger.info("tool.search.vector", hits=len(vector_pm_ids))
            except Exception as e:
                logger.warning("tool.search.vector_error", error=str(e))

        # Phase 2: FTS5 prefix match — always runs, over-fetch to avoid date-truncation
        sql, params = parsed.to_sql(limit=limit * 3, offset=offset)
        try:
            rows = db.execute(sql, params).fetchall()
            for row in rows:
                msg = _row_to_message(row)
                if msg.pm_id not in seen_pm_ids:
                    soft_candidates.append(msg)
                    seen_pm_ids.add(msg.pm_id)
            logger.info("tool.search.fts", hits=len(rows))
        except Exception as e:
            logger.warning("tool.search.fts_error", error=str(e))

        # Phase 3: Guaranteed subject prefix matches (scored for ordering, never filtered)
        guaranteed: list = []
        guaranteed_pm_ids: set[str] = set()
        tokens = [t for t in parsed.raw_fts_terms.split() if len(t) >= 4]
        if tokens:
            like_clauses = " OR ".join("(subject LIKE ? OR subject LIKE ?)" for _ in tokens)
            like_params: list = []
            for t in tokens:
                like_params += [f"{t}%", f"% {t}%"]
            where_filter = f"AND {parsed.where}" if parsed.where != "1" else ""
            subj_sql = f"""
                SELECT rowid, * FROM messages
                WHERE ({like_clauses}) {where_filter}
                GROUP BY message_id
                ORDER BY date DESC
                LIMIT ?
            """
            try:
                subj_rows = db.execute(
                    subj_sql, [*like_params, *parsed.params, limit * 2]
                ).fetchall()
                for row in subj_rows:
                    msg = _row_to_message(row)
                    guaranteed.append(msg)
                    guaranteed_pm_ids.add(msg.pm_id)
                    if msg.pm_id not in seen_pm_ids:
                        seen_pm_ids.add(msg.pm_id)
                logger.info("tool.search.subject_prefix", hits=len(subj_rows))
            except Exception as e:
                logger.warning("tool.search.subject_prefix_error", error=str(e))

        if _embedder:
            try:
                # Score all candidates (soft + guaranteed not already in soft)
                guaranteed_only = [
                    m for m in guaranteed if m.pm_id not in {msg.pm_id for msg in soft_candidates}
                ]
                all_candidates = soft_candidates + guaranteed_only
                scored = _embedder.score(parsed.raw_fts_terms, all_candidates, db)

                top_score = scored[0][0] if scored else 0.0
                logger.info(
                    "tool.search.reranked",
                    candidates=len(all_candidates),
                    guaranteed=len(guaranteed_only),
                    top_score=f"{top_score:.2f}",
                )

                # No cross-encoder threshold — vector distance already filtered
                # gross noise. Reranker orders; we just take top limit.
                scored = apply_bulk_penalty(parsed.raw_fts_terms, scored, db)
                scored = _dedup_conversations(scored)
                results = [msg for _, msg in scored][:limit]
            except Exception as e:
                logger.warning("tool.search.rerank_error", error=str(e))
                results = soft_candidates[:limit]
        else:
            results = soft_candidates[:limit]

    else:
        # No free text — just hard filters, date-sorted
        sql, params = parsed.to_sql(limit=limit, offset=offset)
        try:
            rows = db.execute(sql, params).fetchall()
            results = [_row_to_message(r) for r in rows]
        except Exception as e:
            logger.warning("tool.search.filter_error", error=str(e))

    logger.info("tool.search.done", query=query, count=len(results))
    return [_format_result(r) for r in results]
