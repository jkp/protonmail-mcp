"""LLM-based relevance scoring for search results.

Takes a query and a list of search results (with summaries) and scores
each result's relevance. Uses a single LLM call with all results in
context so the model can make relative judgments.

Results below the threshold are filtered out.
"""

from __future__ import annotations

import asyncio

import httpx
import structlog

logger = structlog.get_logger(__name__)

_MODEL = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
_API_URL = "https://api.together.xyz/v1/chat/completions"
_RELEVANCE_THRESHOLD = 3

# The model loses count when asked for one score per result much past this.
# Measured exact at 20/25/30/40 results, but 45 came back with 46 numbers —
# which fails the length check and silently disables the whole filter. Batches
# are dispatched together, so this costs one round trip rather than several.
_RELEVANCE_BATCH_SIZE = 30

_SYSTEM_PROMPT = """\
You are a search relevance judge. Given a search query and a list of email \
search results with summaries, score each result's relevance to the query.

Use this scale:
1 = completely unrelated, no connection to the query
2 = tangentially related, shares a broad topic but doesn't address the query
3 = somewhat relevant, related to the query but not a direct match
4 = clearly relevant, directly addresses the query topic
5 = highly relevant, exactly what the searcher is looking for

Output ONLY a comma-separated list of scores, one per result, in the same \
order as the input. Example: 5,3,1,4,2

Do not explain your reasoning. Just output the scores."""


def _build_prompt(query: str, results: list[dict]) -> str:
    """One compact line per result — From | Subject | summary."""
    lines = []
    for i, r in enumerate(results, 1):
        summary = r.get("summary", r.get("subject", ""))
        lines.append(f"{i}. From: {r['from']} | Subject: {r['subject']} | {summary}")
    return f"Query: {query}\n\nResults:\n" + "\n".join(lines)


async def score_relevance_raw(query: str, results: list[dict], api_key: str) -> list[int] | None:
    """Score each result 1-5 for relevance. Returns scores in input order, or None.

    Separate from score_relevance so the caller can run it concurrently with the
    cross-encoder reranker — the two take the same query and candidates and are
    independent, so running them in sequence just adds the latencies together.
    """
    if not api_key or not results:
        return None

    batches = [
        results[i : i + _RELEVANCE_BATCH_SIZE]
        for i in range(0, len(results), _RELEVANCE_BATCH_SIZE)
    ]
    responses = await asyncio.gather(
        *(_llm_score(_build_prompt(query, b), api_key, len(b)) for b in batches),
        return_exceptions=True,
    )

    scores: list[int] = []
    for batch, got in zip(batches, responses):
        if isinstance(got, BaseException):
            # ReadTimeout stringifies to "", which made these undiagnosable.
            logger.warning("relevance.score_failed", error=f"{type(got).__name__}: {got}")
            return None
        if not got or len(got) != len(batch):
            logger.warning(
                "relevance.score_mismatch",
                expected=len(batch),
                got=len(got) if got else 0,
            )
            return None
        scores.extend(got)

    return scores


async def score_relevance(
    query: str,
    results: list[dict],
    api_key: str,
    threshold: int = _RELEVANCE_THRESHOLD,
) -> list[dict]:
    """Score and filter search results by relevance to the query.

    Returns only results scoring >= threshold, with relevance_score added.
    If the LLM call fails, returns all results unfiltered.
    """
    if not api_key or not results:
        return results

    scores = await score_relevance_raw(query, results, api_key)
    if scores is None:
        return results

    # Filter and annotate
    filtered = []
    for r, score in zip(results, scores):
        if score >= threshold:
            r["relevance_score"] = score
            filtered.append(r)

    logger.info(
        "relevance.filtered",
        query=query,
        before=len(results),
        after=len(filtered),
        scores=",".join(str(s) for s in scores),
    )

    return filtered if filtered else results[:3]  # Always return at least top 3


async def _llm_score(prompt: str, api_key: str, count: int) -> list[int] | None:
    """Call Together API to score relevance."""
    # One number per result, plus separators. A flat 100 was far too tight: a
    # 45-result search needs 90+ tokens for the numbers alone, so the response
    # came back truncated, the score count fell short, and the caller silently
    # gave up and returned every result unfiltered.
    max_tokens = max(100, count * 8)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": _MODEL,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.0,
            },
            timeout=20,
        )

    if resp.status_code != 200:
        logger.warning("relevance.api_error", status=resp.status_code)
        return None

    data = resp.json()
    text = data["choices"][0]["message"]["content"].strip()

    # Parse comma-separated scores
    try:
        scores = [int(s.strip()) for s in text.split(",")]
        # Clamp to 1-5
        scores = [max(1, min(5, s)) for s in scores]
        return scores
    except (ValueError, AttributeError):
        logger.warning("relevance.parse_failed", raw=text[:100])
        return None
