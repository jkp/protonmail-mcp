"""Batch tools: batch_read, batch_archive, batch_mark_read, batch_delete.

v4: Mutations via ProtonMail native API. SQLite queried for pm_ids and
updated optimistically. No IMAP COPY+DELETE, no notmuch.
"""

import asyncio
from typing import Any

import structlog

from email_mcp.db import _row_to_message
from email_mcp.proton_api import ProtonAPIError, ProtonClient
from email_mcp.query_builder import build_query
from email_mcp.server import db, mcp

logger = structlog.get_logger()

# Module-level ref — set during server lifespan
_api: ProtonClient | None = None

_MAX_SAMPLE_SUBJECTS = 10
# ProtonMail API accepts up to 150 IDs per label/read call.
_API_CHUNK_SIZE = 150
# Maximum messages to affect per tool call (AI loops if more remain).
_MAX_BATCH_SIZE = 300


def _require_api() -> ProtonClient:
    if _api is None:
        raise RuntimeError("ProtonMail API not initialized")
    return _api


def _lookup_pm_ids(message_ids: list[str]) -> tuple[list[str], list[str]]:
    """Map RFC 2822 Message-IDs or pm_ids → pm_ids via SQLite.

    Returns (found_pm_ids, not_found_message_ids).
    """
    found: list[str] = []
    not_found: list[str] = []
    for mid in message_ids:
        row = db.execute(
            "SELECT pm_id FROM messages WHERE message_id = ? OR pm_id = ?",
            [mid, mid],
        ).fetchone()
        if row:
            found.append(row[0])
        else:
            not_found.append(mid)
    return found, not_found


async def _api_label_chunks(pm_ids: list[str], label_id: str) -> tuple[int, list[str]]:
    """Call label_messages in chunks of _API_CHUNK_SIZE. Returns (succeeded, failed_pm_ids)."""
    api = _require_api()
    succeeded = 0
    failed: list[str] = []
    for i in range(0, len(pm_ids), _API_CHUNK_SIZE):
        chunk = pm_ids[i : i + _API_CHUNK_SIZE]
        try:
            await api.label_messages(chunk, label_id)
            succeeded += len(chunk)
        except ProtonAPIError as e:
            logger.warning("batch.label_chunk.failed", label=label_id, error=str(e))
            failed.extend(chunk)
    return succeeded, failed


async def _api_mark_read_chunks(pm_ids: list[str]) -> tuple[int, list[str]]:
    """Call mark_read in chunks of _API_CHUNK_SIZE. Returns (succeeded, failed_pm_ids)."""
    api = _require_api()
    succeeded = 0
    failed: list[str] = []
    for i in range(0, len(pm_ids), _API_CHUNK_SIZE):
        chunk = pm_ids[i : i + _API_CHUNK_SIZE]
        try:
            await api.mark_read(chunk)
            succeeded += len(chunk)
        except ProtonAPIError as e:
            logger.warning("batch.mark_read_chunk.failed", error=str(e))
            failed.extend(chunk)
    return succeeded, failed


def _optimistic_update_folder(pm_ids: list[str], folder: str) -> None:
    from email_mcp.tools.managing import _FOLDER_TO_LABEL
    label_id = _FOLDER_TO_LABEL.get(folder, "")
    for pm_id in pm_ids:
        db.execute(
            "UPDATE messages SET folder = ?, label_ids = ?,"
            " updated_at = unixepoch() WHERE pm_id = ?",
            [folder, f'["{label_id}"]', pm_id],
        )
    db.commit()


def _optimistic_mark_read(pm_ids: list[str]) -> None:
    for pm_id in pm_ids:
        db.execute(
            "UPDATE messages SET unread = 0, updated_at = unixepoch() WHERE pm_id = ?",
            [pm_id],
        )
    db.commit()


# ── Batch read ────────────────────────────────────────────────────────────────


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False, "title": "Batch Read Emails"}
)
async def batch_read(
    message_ids: list[str], folder: str | None = None
) -> list[dict[str, Any]]:
    """Read multiple emails in one call.

    Returns a list of email dicts (same shape as read_email), with
    {"error": ..., "message_id": ...} entries for any that couldn't be found.

    Args:
        message_ids: List of Message-ID header values (or pm_ids) to read
        folder: Ignored in v4
    """
    logger.info("tool.batch_read", count=len(message_ids))
    if not message_ids:
        return []

    async def _read_one(message_id: str) -> dict[str, Any]:
        row = db.execute(
            "SELECT * FROM messages WHERE message_id = ? OR pm_id = ?",
            [message_id, message_id],
        ).fetchone()
        if row is None:
            return {
                "error": "not_found",
                "message_id": message_id,
                "detail": "Message not found in local database.",
            }
        from email_mcp.convert import html_to_markdown

        msg = _row_to_message(row)
        body = db.bodies.get(msg.pm_id) or ""
        if body.strip().startswith(("<", "<!")):
            body = html_to_markdown(body)
        return {
            "message_id": msg.message_id,
            "pm_id": msg.pm_id,
            "from": f"{msg.sender_name} <{msg.sender_email}>" if msg.sender_name else msg.sender_email,
            "to": msg.recipients,
            "subject": msg.subject,
            "date": msg.date,
            "body": body,
            "folder": msg.folder,
            "unread": msg.unread,
            "has_attachments": msg.has_attachments,
        }

    results = await asyncio.gather(*[_read_one(mid) for mid in message_ids])
    logger.info("tool.batch_read.done", count=len(results))
    return list(results)


# ── Batch mutations (by explicit Message-ID list) ─────────────────────────────


@mcp.tool(annotations={"destructiveHint": False, "title": "Batch Archive Emails"})
async def batch_archive(
    message_ids: list[str], folder: str = "INBOX"
) -> dict[str, Any]:
    """Archive multiple emails by Message-ID.

    For bulk operations (e.g. archiving all newsletters), prefer
    search_and_archive which takes a query instead of requiring listing IDs.

    Args:
        message_ids: List of Message-ID header values (or pm_ids) to archive
        folder: Ignored in v4
    """
    logger.info("tool.batch_archive", count=len(message_ids))
    message_ids = message_ids[:_MAX_BATCH_SIZE]
    pm_ids, not_found = _lookup_pm_ids(message_ids)

    succeeded, failed_pm_ids = await _api_label_chunks(pm_ids, "6")
    _optimistic_update_folder([p for p in pm_ids if p not in failed_pm_ids], "Archive")

    errors = [{"pm_id": p, "error": "api_error"} for p in failed_pm_ids]
    errors += [{"message_id": m, "error": "not_found"} for m in not_found]
    logger.info("tool.batch_archive.done", succeeded=succeeded, failed=len(errors))
    return {"status": "completed", "succeeded": succeeded, "failed": len(errors), "errors": errors}


@mcp.tool(annotations={"destructiveHint": False, "title": "Batch Mark Read"})
async def batch_mark_read(
    message_ids: list[str], folder: str | None = None
) -> dict[str, Any]:
    """Mark multiple emails as read by Message-ID.

    For bulk operations prefer search_and_mark_read which takes a query.

    Args:
        message_ids: List of Message-ID header values (or pm_ids) to mark as read
        folder: Ignored in v4
    """
    logger.info("tool.batch_mark_read", count=len(message_ids))
    message_ids = message_ids[:_MAX_BATCH_SIZE]
    pm_ids, not_found = _lookup_pm_ids(message_ids)

    succeeded, failed_pm_ids = await _api_mark_read_chunks(pm_ids)
    _optimistic_mark_read([p for p in pm_ids if p not in failed_pm_ids])

    errors = [{"pm_id": p, "error": "api_error"} for p in failed_pm_ids]
    errors += [{"message_id": m, "error": "not_found"} for m in not_found]
    logger.info("tool.batch_mark_read.done", succeeded=succeeded, failed=len(errors))
    return {"status": "completed", "succeeded": succeeded, "failed": len(errors), "errors": errors}


@mcp.tool(annotations={"destructiveHint": True, "title": "Batch Delete Emails"})
async def batch_delete(
    message_ids: list[str],
    confirm: bool = False,
    folder: str | None = None,
) -> dict[str, Any]:
    """Delete multiple emails by Message-ID (moves to Trash).

    For bulk operations prefer search_and_delete which takes a query.
    Requires confirm=True to execute.

    Args:
        message_ids: List of Message-ID header values (or pm_ids) to delete
        confirm: Must be True to proceed with deletion
        folder: Ignored in v4
    """
    logger.info("tool.batch_delete", count=len(message_ids), confirm=confirm)
    if not confirm:
        return {
            "error": "confirmation_required",
            "detail": "Set confirm=True to delete these messages.",
        }

    message_ids = message_ids[:_MAX_BATCH_SIZE]
    pm_ids, not_found = _lookup_pm_ids(message_ids)

    succeeded, failed_pm_ids = await _api_label_chunks(pm_ids, "3")
    _optimistic_update_folder([p for p in pm_ids if p not in failed_pm_ids], "Trash")

    errors = [{"pm_id": p, "error": "api_error"} for p in failed_pm_ids]
    errors += [{"message_id": m, "error": "not_found"} for m in not_found]
    logger.info("tool.batch_delete.done", succeeded=succeeded, failed=len(errors))
    return {"status": "completed", "succeeded": succeeded, "failed": len(errors), "errors": errors}


# ── Query-based batch operations ──────────────────────────────────────────────

def _query_to_pm_ids(
    query: str,
    skip_folder: str | None = None,
    limit: int = _MAX_BATCH_SIZE,
) -> tuple[list[str], list[str], int]:
    """Run Gmail-style query against SQLite, return (pm_ids, subjects, total_matched).

    Args:
        query: Gmail-style query string
        skip_folder: Exclude messages in this folder (e.g. "Trash" for search_and_delete)
        limit: Max results to return (for batching)
    """
    parsed = build_query(query)

    # Count total matching (without limit) for dry_run
    count_sql, count_params = parsed.to_sql(limit=10_000, offset=0)
    all_rows = db.execute(count_sql, count_params).fetchall()
    if skip_folder:
        all_rows = [r for r in all_rows if r["folder"] != skip_folder]

    total = len(all_rows)
    batch = all_rows[:limit]

    pm_ids = [r["pm_id"] for r in batch]
    subjects = [r["subject"] or "" for r in batch]
    return pm_ids, subjects, total


@mcp.tool(annotations={"destructiveHint": False, "title": "Search and Mark Read"})
async def search_and_mark_read(query: str, dry_run: bool = True) -> dict[str, Any]:
    """Mark all emails matching a search query as read.

    Workflow: call with dry_run=True first to preview (count + sample subjects),
    then dry_run=False to execute. Calls again with the same query if remaining > 0.

    Args:
        query: Gmail-style search query (e.g. "from:newsletter is:unread")
        dry_run: If True (default), preview without acting
    """
    logger.info("tool.search_and_mark_read", query=query, dry_run=dry_run)
    try:
        pm_ids, subjects, total = _query_to_pm_ids(query)
    except Exception as e:
        return {"error": "query_failed", "detail": str(e)}

    if dry_run:
        return {
            "would_affect": total,
            "sample_subjects": subjects[:_MAX_SAMPLE_SUBJECTS],
        }

    succeeded, failed_pm_ids = await _api_mark_read_chunks(pm_ids)
    _optimistic_mark_read([p for p in pm_ids if p not in failed_pm_ids])

    remaining = max(0, total - len(pm_ids))
    logger.info("tool.search_and_mark_read.done", succeeded=succeeded, remaining=remaining)
    return {
        "succeeded": succeeded,
        "failed": len(failed_pm_ids),
        "remaining": remaining,
    }


@mcp.tool(annotations={"destructiveHint": False, "title": "Search and Archive"})
async def search_and_archive(query: str, dry_run: bool = True) -> dict[str, Any]:
    """Archive all emails matching a search query.

    Workflow: call with dry_run=True first to preview (count + sample subjects),
    then dry_run=False to execute. Calls again with the same query if remaining > 0.

    Args:
        query: Gmail-style search query (e.g. "from:newsletter older_than:30d")
        dry_run: If True (default), preview without acting
    """
    logger.info("tool.search_and_archive", query=query, dry_run=dry_run)
    try:
        pm_ids, subjects, total = _query_to_pm_ids(query)
    except Exception as e:
        return {"error": "query_failed", "detail": str(e)}

    if dry_run:
        return {
            "would_affect": total,
            "sample_subjects": subjects[:_MAX_SAMPLE_SUBJECTS],
        }

    succeeded, failed_pm_ids = await _api_label_chunks(pm_ids, "6")
    _optimistic_update_folder([p for p in pm_ids if p not in failed_pm_ids], "Archive")

    remaining = max(0, total - len(pm_ids))
    logger.info("tool.search_and_archive.done", succeeded=succeeded, remaining=remaining)
    return {
        "succeeded": succeeded,
        "failed": len(failed_pm_ids),
        "remaining": remaining,
    }


@mcp.tool(annotations={"destructiveHint": True, "title": "Search and Delete"})
async def search_and_delete(query: str, dry_run: bool = True) -> dict[str, Any]:
    """Delete all emails matching a search query (move to Trash).

    Workflow: call with dry_run=True first to preview (count + sample subjects),
    then dry_run=False to execute. Calls again with the same query if remaining > 0.

    Args:
        query: Gmail-style search query (e.g. "from:spam subject:unsubscribe")
        dry_run: If True (default), preview without acting
    """
    logger.info("tool.search_and_delete", query=query, dry_run=dry_run)
    try:
        pm_ids, subjects, total = _query_to_pm_ids(query, skip_folder="Trash")
    except Exception as e:
        return {"error": "query_failed", "detail": str(e)}

    if dry_run:
        return {
            "would_affect": total,
            "sample_subjects": subjects[:_MAX_SAMPLE_SUBJECTS],
        }

    succeeded, failed_pm_ids = await _api_label_chunks(pm_ids, "3")
    _optimistic_update_folder([p for p in pm_ids if p not in failed_pm_ids], "Trash")

    remaining = max(0, total - len(pm_ids))
    logger.info("tool.search_and_delete.done", succeeded=succeeded, remaining=remaining)
    return {
        "succeeded": succeeded,
        "failed": len(failed_pm_ids),
        "remaining": remaining,
    }
