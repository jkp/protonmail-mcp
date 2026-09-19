"""Search tool using FTS5 + semantic vector search."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog

from email_mcp.db import _row_to_message
from email_mcp.embedder import Embedder
from email_mcp.query_builder import build_query
from email_mcp.relevance import _RELEVANCE_THRESHOLD, score_relevance, score_relevance_raw
from email_mcp.server import db, mcp, settings
from email_mcp.summarizer import summarize_messages
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

# How many distinct candidates to send through the cross-encoder. Reranking is
# the dominant cost (~0.08s each) and the pool grows with `limit`, so a
# 50-result request would otherwise score ~45 items to order a list nobody
# reads that far down. Everything past this stays in recall order behind the
# reranked head, so no candidate is dropped from the response.
_RERANK_MAX_CANDIDATES = 30


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


async def _score_candidates(
    query: str, msgs: list, db_ref: Any, api_key: str
) -> tuple[dict[str, int], dict[str, str]]:
    """Summarize candidates and fetch raw relevance scores.

    Returns ({pm_id: score}, {pm_id: summary}). Scores come back empty if the
    LLM call fails, and the caller falls back to reranker order.
    """
    if not api_key or not msgs:
        return {}, {}

    summaries = await summarize_messages([m.pm_id for m in msgs], db_ref, api_key=api_key)
    results = [_format_result(m, summaries.get(m.pm_id)) for m in msgs]
    scores = await score_relevance_raw(query, results, api_key)
    if scores is None:
        return {}, summaries
    return dict(zip((m.pm_id for m in msgs), scores)), summaries


def _apply_relevance_filter(
    formatted: list[dict[str, Any]], results: list, rel_scores: dict[str, int]
) -> list[dict[str, Any]]:
    """Drop results below threshold, annotating the survivors.

    Mirrors score_relevance: keep everything >= threshold, and if nothing
    qualifies keep the top 3 rather than returning an empty list.
    """
    paired = [(f, rel_scores.get(r.pm_id, 0)) for f, r in zip(formatted, results)]
    kept = [(f, s) for f, s in paired if s >= _RELEVANCE_THRESHOLD]
    if not kept:
        kept = paired[:3]
    for f, s in kept:
        f["relevance_score"] = s
    logger.info(
        "relevance.filtered",
        before=len(paired),
        after=len(kept),
        scores=",".join(str(s) for _, s in paired),
    )
    return [f for f, _ in kept]


def _format_date(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_result(r, summary: str | None = None) -> dict[str, Any]:
    result = {
        "id": r.row_id,
        "from": (f"{r.sender_name} <{r.sender_email}>" if r.sender_name else r.sender_email),
        "subject": r.subject,
        "date": _format_date(r.date),
        "folder": r.folder,
        "unread": r.unread,
        "has_attachments": r.has_attachments,
        "web_url": _web_url(r.conversation_id, r.folder),
    }
    if summary:
        result["summary"] = summary
    return result


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
    summaries: dict[str, str] = {}
    # Filled by the parallel scoring step so relevance is never computed twice.
    rel_scores: dict[str, int] = {}
    # Set when the parallel step already tried relevance, so a failure falls back
    # to reranker order instead of paying for a second, serial LLM call.
    relevance_attempted = False

    if parsed.raw_fts_terms:
        soft_candidates: list = []
        seen_pm_ids: set[str] = set()

        # Phase 1: Vector search
        if _embedder:
            try:
                if parsed.where_clauses:
                    vector_pm_ids = await asyncio.to_thread(
                        _embedder.search_with_filters,
                        parsed.raw_fts_terms,
                        parsed.where,
                        parsed.params,
                        limit,
                    )
                else:
                    vector_pm_ids = await asyncio.to_thread(
                        _embedder.search, parsed.raw_fts_terms, limit
                    )

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
            rows = await asyncio.to_thread(lambda: db.execute(sql, params).fetchall())
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
                subj_params = [*like_params, *parsed.params, limit * 2]
                subj_rows = await asyncio.to_thread(
                    lambda: db.execute(subj_sql, subj_params).fetchall()
                )
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

                # Collapse thread replies and repeated templates BEFORE reranking.
                # Dedup discards ~70% of candidates (150 -> 44 in production) and
                # the cross-encoder is by far the most expensive stage, so most of
                # what it was scoring got thrown away moments later. Candidates
                # arrive recall-best-first, so first-seen wins exactly as it did
                # when this ran after the rerank.
                pool = len(all_candidates)
                all_candidates = [
                    m for _, m in _dedup_conversations([(0.0, m) for m in all_candidates])
                ]

                # The cross-encoder dominates search latency and scales with the
                # candidate count, which grows with `limit` (a 50-result client
                # asks produce ~45 distinct candidates). Score a bounded head and
                # leave the remainder in recall order beneath every reranked hit:
                # the head is what actually gets read, and the result count the
                # caller asked for is preserved.
                head = all_candidates[:_RERANK_MAX_CANDIDATES]
                tail = all_candidates[_RERANK_MAX_CANDIDATES:]

                # The two scoring passes are independent — each takes the query
                # and candidates and returns its own scores — so run them
                # together. In sequence they cost their sum (~5s); concurrent
                # the stage costs the slower of the two (~2.5s).
                relevance_attempted = bool(settings.together_api_key)
                scored, (rel_scores, summaries) = await asyncio.gather(
                    asyncio.to_thread(_embedder.score, parsed.raw_fts_terms, head, db),
                    _score_candidates(
                        parsed.raw_fts_terms, all_candidates, db, settings.together_api_key
                    ),
                )

                if tail:
                    floor = min((s for s, _ in scored), default=0.0) - 1.0
                    scored = scored + [(floor - i, m) for i, m in enumerate(tail)]

                # Relevance is the coarser but more reliable signal (a 1-5 from
                # the LLM); the cross-encoder breaks ties inside a band. Ordering
                # on the reranker alone stranded genuinely relevant mail at
                # position 20+ whenever its logits came back flat at 0.00.
                scored.sort(key=lambda t: (-rel_scores.get(t[1].pm_id, 0), -t[0]))

                top_score = scored[0][0] if scored else 0.0
                logger.info(
                    "tool.search.scored",
                    candidates=len(head),
                    tail=len(tail),
                    pool=pool,
                    guaranteed=len(guaranteed_only),
                    relevance_scored=len(rel_scores),
                    top_score=f"{top_score:.2f}",
                )

                # No cross-encoder threshold — vector distance already filtered
                # gross noise. Reranker orders; we just take top limit.
                scored = apply_bulk_penalty(parsed.raw_fts_terms, scored, db)
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
            rows = await asyncio.to_thread(lambda: db.execute(sql, params).fetchall())
            results = [_row_to_message(r) for r in rows]
        except Exception as e:
            logger.warning("tool.search.filter_error", error=str(e))

    # Summaries normally arrive with the parallel scoring step; only fetch them
    # here when that step didn't run (filter-only queries, or no embedder).
    if results and settings.together_api_key and not summaries:
        try:
            summaries = await summarize_messages(
                [r.pm_id for r in results], db, api_key=settings.together_api_key
            )
        except Exception as e:
            logger.warning("tool.search.summarize_error", error=str(e))

    formatted = [_format_result(r, summaries.get(r.pm_id)) for r in results]

    # LLM relevance filter — drop the noise. Skip the call when the parallel
    # step already produced scores for these candidates.
    if formatted and parsed.raw_fts_terms:
        if rel_scores:
            formatted = _apply_relevance_filter(formatted, results, rel_scores)
        elif settings.together_api_key and not relevance_attempted:
            try:
                formatted = await score_relevance(
                    parsed.raw_fts_terms, formatted, api_key=settings.together_api_key
                )
            except Exception as e:
                logger.warning("tool.search.relevance_error", error=str(e))

    logger.info("tool.search.done", query=query, count=len(formatted))
    return formatted
