"""Batch tools: batch_read, batch_archive, batch_mark_read, batch_delete.

Batch operations reduce MCP round-trips from O(N) to O(1) for inbox triage.
Query-based batch tools push filtering to the server via notmuch search.
"""

import asyncio
from typing import Any

import structlog

from email_mcp.db import _row_to_message
from email_mcp.imap import ImapError, ImapMutator
from email_mcp.search import NotmuchError, NotmuchSearcher, translate_query
from email_mcp.server import db, mcp
from email_mcp.store import MaildirStore
from email_mcp.sync import SyncEngine

logger = structlog.get_logger()

# Module-level refs — set during server lifespan
_imap: ImapMutator | None = None
_sync_engine: SyncEngine | None = None
_store: MaildirStore | None = None
_searcher: NotmuchSearcher | None = None


_MAX_SAMPLE_SUBJECTS = 10
# ProtonMail's API processes up to 150 messages per request, but each
# message takes ~500ms server-side (COPY) or ~85ms (SEARCH) through Bridge.
# We cap at 20 for quick feedback — the AI calls again for the next batch.
_MAX_BATCH_SIZE = 20



def _require_searcher() -> NotmuchSearcher:
    if _searcher is None:
        raise RuntimeError("Searcher not initialized")
    return _searcher


def _require_imap() -> ImapMutator:
    if _imap is None:
        raise RuntimeError("IMAP mutator not initialized")
    return _imap


def _require_sync() -> SyncEngine:
    if _sync_engine is None:
        raise RuntimeError("Sync engine not initialized")
    return _sync_engine


def _require_store() -> MaildirStore:
    if _store is None:
        raise RuntimeError("Store not initialized")
    return _store


def _email_to_dict(email) -> dict[str, Any]:
    """Convert a MessageRow to the same dict shape as read_email."""
    body = db.bodies.get(email.pm_id) or ""
    return {
        "message_id": email.message_id,
        "pm_id": email.pm_id,
        "from": f"{email.sender_name} <{email.sender_email}>" if email.sender_name else email.sender_email,
        "to": email.recipients,
        "subject": email.subject,
        "date": email.date,
        "body": body,
        "folder": email.folder,
        "unread": email.unread,
        "has_attachments": email.has_attachments,
    }


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "title": "Batch Read Emails",
    }
)
async def batch_read(
    message_ids: list[str], folder: str | None = None
) -> list[dict[str, Any]]:
    """Read multiple emails in one call.

    Returns a list of email dicts (same shape as read_email), with
    {"error": ..., "message_id": ...} entries for any that couldn't be found.

    Args:
        message_ids: List of Message-ID header values to read
        folder: Optional folder hint to speed up lookup
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
        return _email_to_dict(_row_to_message(row))

    results = await asyncio.gather(*[_read_one(mid) for mid in message_ids])
    logger.info("tool.batch_read.done", count=len(results))
    return list(results)


@mcp.tool(
    annotations={"destructiveHint": False, "title": "Batch Archive Emails"}
)
async def batch_archive(
    message_ids: list[str], folder: str = "INBOX"
) -> dict[str, Any]:
    """Archive multiple emails by Message-ID.

    For bulk operations (e.g. archiving all newsletters), prefer
    search_and_archive which takes a query and doesn't require listing IDs.

    Args:
        message_ids: List of Message-ID header values to archive
        folder: Folder the emails are in (required for fast IMAP lookup)
    """
    logger.info("tool.batch_archive", count=len(message_ids), folder=folder)
    total = len(message_ids)
    message_ids = message_ids[:_MAX_BATCH_SIZE]
    imap = _require_imap()
    local_store = _require_store()
    sync = _require_sync()

    try:
        succeeded, errors = await imap.batch_archive(message_ids, from_folder=folder)
    except (ImapError, Exception) as e:
        logger.error("tool.batch_archive.imap_failed", error=str(e))
        return {"error": "imap_error", "detail": str(e)}

    for mid in message_ids:
        if not any(e["message_id"] == mid for e in errors):
            local_store.optimistic_move(mid, "Archive", folder)

    remaining = total - len(message_ids)
    sync.request_reindex()
    logger.info("tool.batch_archive.done", succeeded=succeeded, failed=len(errors))
    return {
        "status": "completed",
        "succeeded": succeeded,
        "failed": len(errors),
        "errors": errors,
        "remaining": remaining,
    }


@mcp.tool(
    annotations={"destructiveHint": False, "title": "Batch Mark Read"}
)
async def batch_mark_read(
    message_ids: list[str], folder: str | None = None
) -> dict[str, Any]:
    r"""Mark multiple emails as read by Message-ID.

    For bulk operations (e.g. marking all newsletters read), prefer
    search_and_mark_read which takes a query and doesn't require listing IDs.

    Args:
        message_ids: List of Message-ID header values to mark as read
        folder: Current folder hint (optional)
    """
    logger.info("tool.batch_mark_read", count=len(message_ids), folder=folder)
    total = len(message_ids)
    message_ids = message_ids[:_MAX_BATCH_SIZE]
    imap = _require_imap()

    try:
        succeeded, errors = await imap.batch_add_flags(
            message_ids, r"\Seen", folder=folder
        )
    except (ImapError, Exception) as e:
        logger.error("tool.batch_mark_read.imap_failed", error=str(e))
        return {"error": "imap_error", "detail": str(e)}

    remaining = total - len(message_ids)
    logger.info("tool.batch_mark_read.done", succeeded=succeeded, failed=len(errors))
    return {
        "status": "completed",
        "succeeded": succeeded,
        "failed": len(errors),
        "errors": errors,
        "remaining": remaining,
    }


@mcp.tool(
    annotations={"destructiveHint": True, "title": "Batch Delete Emails"}
)
async def batch_delete(
    message_ids: list[str],
    confirm: bool = False,
    folder: str | None = None,
) -> dict[str, Any]:
    """Delete multiple emails by Message-ID (moves to Trash).

    For bulk operations (e.g. deleting all spam), prefer search_and_delete
    which takes a query and doesn't require listing IDs.

    Requires confirm=True to execute. Returns an error if confirm is False.

    Args:
        message_ids: List of Message-ID header values to delete
        confirm: Must be True to proceed with deletion
        folder: Current folder hint (optional)
    """
    logger.info("tool.batch_delete", count=len(message_ids), confirm=confirm)
    total = len(message_ids)
    message_ids = message_ids[:_MAX_BATCH_SIZE]
    if not confirm:
        return {
            "error": "confirmation_required",
            "detail": "Set confirm=True to delete these messages.",
            "message_ids": message_ids,
        }

    imap = _require_imap()
    local_store = _require_store()
    sync = _require_sync()

    try:
        succeeded, errors = await imap.batch_delete(message_ids, from_folder=folder)
    except (ImapError, Exception) as e:
        logger.error("tool.batch_delete.imap_failed", error=str(e))
        return {"error": "imap_error", "detail": str(e)}

    for mid in message_ids:
        if not any(e["message_id"] == mid for e in errors):
            local_store.optimistic_move(mid, "Trash", folder)

    remaining = total - len(message_ids)
    sync.request_reindex()
    logger.info("tool.batch_delete.done", succeeded=succeeded, failed=len(errors))
    return {
        "status": "completed",
        "succeeded": succeeded,
        "failed": len(errors),
        "errors": errors,
        "remaining": remaining,
    }


# ── Query-based batch operations ─────────────────────────────────────


async def _search_to_folder_groups(
    query: str,
) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    """Run notmuch search and group results by folder.

    Returns:
        (ids_by_folder, results_as_dicts) where ids_by_folder is
        {folder: [message_id, ...]} and results_as_dicts has subject info
        for dry-run reporting.
    """
    searcher = _require_searcher()
    translated = translate_query(query)
    results = await searcher.search(translated)

    # "All Mail" is a virtual folder containing every message — searching it
    # via IMAP is ~100x slower than real folders. Filter it out; every message
    # in "All Mail" also exists in a real folder.
    _SKIP_FOLDERS = {"All Mail"}

    ids_by_folder: dict[str, list[str]] = {}
    for r in results:
        folders = [f for f in r.folders if f not in _SKIP_FOLDERS] or r.folders or ["INBOX"]
        for folder in folders:
            ids_by_folder.setdefault(folder, []).append(r.message_id)

    return ids_by_folder, [
        {"message_id": r.message_id, "folders": r.folders, "subject": r.subject}
        for r in results
    ]


def _truncate_to_batch_size(
    ids_by_folder: dict[str, list[str]],
    results: list[dict[str, Any]],
) -> tuple[dict[str, list[str]], list[dict[str, Any]], int]:
    """Truncate results to _MAX_BATCH_SIZE, preserving folder grouping.

    Returns (truncated_ids_by_folder, truncated_results, total_before_truncation).
    """
    total = len(results)
    if total <= _MAX_BATCH_SIZE:
        return ids_by_folder, results, total

    # Take the first _MAX_BATCH_SIZE results
    kept = results[:_MAX_BATCH_SIZE]
    kept_ids = {r["message_id"] for r in kept}

    truncated: dict[str, list[str]] = {}
    for folder, mids in ids_by_folder.items():
        filtered = [mid for mid in mids if mid in kept_ids]
        if filtered:
            truncated[folder] = filtered

    return truncated, kept, total


def _dry_run_response(
    results: list[dict[str, Any]],
    ids_by_folder: dict[str, list[str]],
) -> dict[str, Any]:
    """Build a dry-run response with count, sample subjects, and folder breakdown."""
    subjects = [r["subject"] for r in results if r.get("subject")]
    by_folder = {folder: len(ids) for folder, ids in sorted(ids_by_folder.items())}
    return {
        "would_affect": len(results),
        "sample_subjects": subjects[:_MAX_SAMPLE_SUBJECTS],
        "by_folder": by_folder,
    }


async def _execute_query_batch(
    tool_name: str,
    ids_by_folder: dict[str, list[str]],
    results: list[dict[str, Any]],
    imap_op: Any,
    move_target: str | None = None,
    skip_source_folders: set[str] | None = None,
) -> dict[str, Any]:
    """Shared execution path for all query-based batch tools.

    Truncates to batch size, runs the given IMAP operation, applies optimistic
    local moves (if move_target is set), guards against infinite loops when the
    batch makes zero progress, and requests a reindex.

    Args:
        tool_name: Used for structured log keys.
        ids_by_folder: Folder-grouped message IDs from _search_to_folder_groups.
        results: Flat result list (same messages, for batch-size tracking).
        imap_op: Async callable(ids_by_folder) → (succeeded, errors).
        move_target: If set, apply optimistic local moves to this folder.
        skip_source_folders: Source folders to exclude before operating
            (e.g. {"Trash"} for delete, to avoid Trash→Trash COPY).
    """
    if skip_source_folders:
        ids_by_folder = {
            f: ids for f, ids in ids_by_folder.items()
            if f not in skip_source_folders
        }

    if not ids_by_folder:
        return {"succeeded": 0, "failed": 0, "errors": [], "remaining": 0}

    ids_by_folder, results, total = _truncate_to_batch_size(ids_by_folder, results)

    succeeded, errors = await imap_op(ids_by_folder)

    if move_target:
        local_store = _require_store()
        failed_ids = {e["message_id"] for e in errors}
        for r in results:
            if r["message_id"] not in failed_ids:
                for folder in r["folders"] or ["INBOX"]:
                    local_store.optimistic_move(r["message_id"], move_target, folder)
        _require_sync().request_reindex()

    remaining = total - len(results)
    # Zero-progress guard: stale notmuch entries that don't exist in IMAP
    # would loop forever. Stop and let the next sync clean up the index.
    if succeeded == 0 and errors:
        remaining = 0

    logger.info(
        f"tool.{tool_name}.done",
        succeeded=succeeded,
        failed=len(errors),
        remaining=remaining,
    )
    return {
        "succeeded": succeeded,
        "failed": len(errors),
        "errors": errors,
        "remaining": remaining,
    }


@mcp.tool(
    annotations={"destructiveHint": False, "title": "Search and Mark Read"}
)
async def search_and_mark_read(
    query: str, dry_run: bool = True
) -> dict[str, Any]:
    """Mark all emails matching a search query as read.

    Preferred over batch_mark_read for bulk operations — takes a query
    instead of requiring individual Message-IDs.

    Workflow: call with dry_run=True first to preview (returns count,
    sample subjects, and per-folder breakdown), then dry_run=False to execute.
    Processes up to {batch_size} messages per call. If more match, the response
    includes "remaining" — call again with the same query to continue.

    Args:
        query: Gmail-style search query (e.g. "from:newsletter", "is:unread in:inbox")
        dry_run: If True (default), preview what would be affected without acting
    """
    logger.info("tool.search_and_mark_read", query=query, dry_run=dry_run)
    try:
        ids_by_folder, results = await _search_to_folder_groups(query)
    except (NotmuchError, Exception) as e:
        logger.error("tool.search_and_mark_read.search_failed", error=str(e))
        return {"error": "search_failed", "detail": str(e)}

    if dry_run:
        return _dry_run_response(results, ids_by_folder)

    imap = _require_imap()
    return await _execute_query_batch(
        "search_and_mark_read",
        ids_by_folder,
        results,
        imap_op=lambda g: imap.batch_add_flags_by_folder(g, [r"\Seen"]),
    )


@mcp.tool(
    annotations={"destructiveHint": False, "title": "Search and Archive"}
)
async def search_and_archive(
    query: str, dry_run: bool = True
) -> dict[str, Any]:
    """Archive all emails matching a search query.

    Preferred over batch_archive for bulk operations — takes a query
    instead of requiring individual Message-IDs.

    Workflow: call with dry_run=True first to preview (returns count,
    sample subjects, and per-folder breakdown), then dry_run=False to execute.
    Processes up to {batch_size} messages per call. If more match, the response
    includes "remaining" — call again with the same query to continue.

    Args:
        query: Gmail-style search query (e.g. "from:newsletter", "older_than:30d")
        dry_run: If True (default), preview what would be affected without acting
    """
    logger.info("tool.search_and_archive", query=query, dry_run=dry_run)
    try:
        ids_by_folder, results = await _search_to_folder_groups(query)
    except (NotmuchError, Exception) as e:
        logger.error("tool.search_and_archive.search_failed", error=str(e))
        return {"error": "search_failed", "detail": str(e)}

    if dry_run:
        return _dry_run_response(results, ids_by_folder)

    imap = _require_imap()
    return await _execute_query_batch(
        "search_and_archive",
        ids_by_folder,
        results,
        imap_op=lambda g: imap.batch_move_by_folder(g, "Archive"),
        move_target="Archive",
    )


@mcp.tool(
    annotations={"destructiveHint": True, "title": "Search and Delete"}
)
async def search_and_delete(
    query: str, dry_run: bool = True
) -> dict[str, Any]:
    """Delete all emails matching a search query (move to Trash).

    Preferred over batch_delete for bulk operations — takes a query
    instead of requiring individual Message-IDs.

    Workflow: call with dry_run=True first to preview (returns count,
    sample subjects, and per-folder breakdown), then dry_run=False to execute.
    Processes up to {batch_size} messages per call. If more match, the response
    includes "remaining" — call again with the same query to continue.

    Args:
        query: Gmail-style search query (e.g. "from:spam", "subject:unsubscribe")
        dry_run: If True (default), preview what would be affected without acting
    """
    logger.info("tool.search_and_delete", query=query, dry_run=dry_run)
    try:
        ids_by_folder, results = await _search_to_folder_groups(query)
    except (NotmuchError, Exception) as e:
        logger.error("tool.search_and_delete.search_failed", error=str(e))
        return {"error": "search_failed", "detail": str(e)}

    if dry_run:
        return _dry_run_response(results, ids_by_folder)

    imap = _require_imap()
    return await _execute_query_batch(
        "search_and_delete",
        ids_by_folder,
        results,
        imap_op=lambda g: imap.batch_move_by_folder(g, "Trash"),
        move_target="Trash",
        # Messages already in Trash: Trash→Trash COPY rejected by Bridge (Code=2501)
        skip_source_folders={"Trash"},
    )


# Interpolate batch size into docstrings so it's defined in one place
for _fn in (search_and_mark_read, search_and_archive, search_and_delete):
    if _fn.__doc__:
        _fn.__doc__ = _fn.__doc__.format(batch_size=_MAX_BATCH_SIZE)
