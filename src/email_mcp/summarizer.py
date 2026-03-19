"""Lazy email summarizer using Together API.

Generates 2-3 sentence summaries of emails on demand, caches in the
summary column of the messages table. Uses a fast model (Llama 3.3 70B
Turbo) for low latency and cost.

Summaries are generated in parallel for search results that don't have
one cached yet. Typical wall time: <500ms for 20 emails.
"""

from __future__ import annotations

import asyncio

import httpx
import structlog

from email_mcp.convert import body_for_display
from email_mcp.db import Database

logger = structlog.get_logger(__name__)

_MODEL = "mistralai/Mistral-Small-24B-Instruct-2501"
_API_URL = "https://api.together.xyz/v1/chat/completions"
_MAX_BODY_CHARS = 2000

_SYSTEM_PROMPT = (
    "Summarize the following email in 2-3 sentences. "
    "Focus on the key information: who, what, and any action items. "
    "Be concise and factual. Do not include greetings or sign-offs."
)


async def summarize_messages(
    pm_ids: list[str],
    db: Database,
    api_key: str,
) -> dict[str, str]:
    """Summarize emails, using cached summaries where available.

    Returns {pm_id: summary} for all messages that have or get a summary.
    Messages without bodies are skipped.
    """
    if not api_key or not pm_ids:
        return {}

    result: dict[str, str] = {}
    needs_llm: list[str] = []

    # Check cache
    placeholders = ",".join("?" * len(pm_ids))
    rows = db.execute(
        f"SELECT pm_id, summary FROM messages WHERE pm_id IN ({placeholders})",
        list(pm_ids),
    ).fetchall()

    cached = {r[0]: r[1] for r in rows if r[1] is not None}
    result.update(cached)
    needs_llm = [pid for pid in pm_ids if pid not in cached]

    if not needs_llm:
        return result

    # Build prompts for uncached messages
    prompts: dict[str, str] = {}
    for pm_id in needs_llm:
        body = db.bodies.get(pm_id)
        if not body:
            continue
        msg = db.messages.get(pm_id)
        if not msg:
            continue

        plain = body_for_display(body[:_MAX_BODY_CHARS])
        sender = msg.sender_name or msg.sender_email or ""
        subject = msg.subject or ""
        prompts[pm_id] = f"From: {sender}\nSubject: {subject}\n\n{plain}"

    if not prompts:
        return result

    # Fire parallel LLM calls
    summaries = await _batch_summarize(prompts, api_key)

    # Cache and return
    for pm_id, summary in summaries.items():
        db.execute(
            "UPDATE messages SET summary = ? WHERE pm_id = ?",
            [summary, pm_id],
        )
        result[pm_id] = summary
    if summaries:
        db.commit()
        logger.info("summarizer.cached", count=len(summaries))

    return result


async def _batch_summarize(
    prompts: dict[str, str],
    api_key: str,
) -> dict[str, str]:
    """Call Together API in parallel for all prompts."""
    results: dict[str, str] = {}

    async def _call_one(pm_id: str, text: str) -> None:
        try:
            summary = await _llm_summarize(text, api_key)
            if summary:
                results[pm_id] = summary
        except Exception as e:
            logger.warning("summarizer.call_failed", pm_id=pm_id, error=str(e))

    async with httpx.AsyncClient() as _:
        await asyncio.gather(*[_call_one(pid, text) for pid, text in prompts.items()])

    return results


async def _llm_summarize(text: str, api_key: str) -> str | None:
    """Call Together API for a single summarization."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": _MODEL,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                "max_tokens": 150,
                "temperature": 0.0,
            },
            timeout=10,
        )

    if resp.status_code != 200:
        logger.warning("summarizer.api_error", status=resp.status_code)
        return None

    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()
