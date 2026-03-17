"""Batch tools: batch_read, batch_archive, batch_mark_read, batch_delete.

Batch operations reduce MCP round-trips from O(N) to O(1) for inbox triage.
Query-based batch tools push filtering to the server via notmuch search.
"""

import asyncio
from typing import Any

import structlog

from email_mcp.imap import ImapError, ImapMutator
from email_mcp.search import NotmuchError, NotmuchSearcher, translate_query
from email_mcp.server import mcp
from email_mcp.store import MaildirStore
from email_mcp.sync import SyncEngine
from email_mcp.tools.reading import _resolve_email

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
    """Convert an Email model to the same dict shape as read_email."""
    body = email.body_html if email.body_html else email.body_plain
    return {
        "message_id": email.message_id,
        "from": str(email.from_),
        "to": [str(addr) for addr in email.to],
        "cc": [str(addr) for addr in email.cc],
        "subject": email.subject,
        "date": email.date_str,
        "body": body,
        "folder": email.folder,
        "flags": email.flags,
        "attachments": [
            {"filename": a.filename, "content_type": a.content_type, "size": a.size}
            for a in email.attachments
        ],
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
        email = await _resolve_email(message_id, folder)
        if email is None:
            return {
                "error": "not_found_locally",
                "message_id": message_id,
                "detail": "This email may have been recently moved. Try again in ~60s.",
            }
        return _email_to_dict(email)

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

    if not ids_by_folder:
        return {"succeeded": 0, "failed": 0, "errors": [], "remaining": 0}

    ids_by_folder, results, total = _truncate_to_batch_size(
        ids_by_folder, results
    )

    imap = _require_imap()
    succeeded, errors = await imap.batch_add_flags_by_folder(
        ids_by_folder, [r"\Seen"]
    )

    remaining = total - len(results)
    logger.info(
        "tool.search_and_mark_read.done",
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

    if not ids_by_folder:
        return {"succeeded": 0, "failed": 0, "errors": [], "remaining": 0}

    ids_by_folder, results, total = _truncate_to_batch_size(
        ids_by_folder, results
    )

    imap = _require_imap()
    local_store = _require_store()
    sync = _require_sync()

    succeeded, errors = await imap.batch_move_by_folder(ids_by_folder, "Archive")

    # Optimistic local moves for succeeded messages
    failed_ids = {e["message_id"] for e in errors}
    for r in results:
        if r["message_id"] not in failed_ids:
            for folder in r["folders"] or ["INBOX"]:
                local_store.optimistic_move(r["message_id"], "Archive", folder)

    remaining = total - len(results)
    sync.request_reindex()
    logger.info(
        "tool.search_and_archive.done",
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

    if not ids_by_folder:
        return {"succeeded": 0, "failed": 0, "errors": [], "remaining": 0}

    ids_by_folder, results, total = _truncate_to_batch_size(
        ids_by_folder, results
    )

    imap = _require_imap()
    local_store = _require_store()
    sync = _require_sync()

    succeeded, errors = await imap.batch_move_by_folder(ids_by_folder, "Trash")

    failed_ids = {e["message_id"] for e in errors}
    for r in results:
        if r["message_id"] not in failed_ids:
            for folder in r["folders"] or ["INBOX"]:
                local_store.optimistic_move(r["message_id"], "Trash", folder)

    remaining = total - len(results)
    sync.request_reindex()
    logger.info(
        "tool.search_and_delete.done",
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


# Interpolate batch size into docstrings so it's defined in one place
for _fn in (search_and_mark_read, search_and_archive, search_and_delete):
    if _fn.__doc__:
        _fn.__doc__ = _fn.__doc__.format(batch_size=_MAX_BATCH_SIZE)
