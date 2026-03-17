"""Batch tools: batch_read, batch_archive, batch_mark_read, batch_delete.

Batch operations reduce MCP round-trips from O(N) to O(1) for inbox triage.
"""

import asyncio
from typing import Any

import structlog

from email_mcp.imap import ImapError, ImapMutator
from email_mcp.server import mcp
from email_mcp.store import MaildirStore
from email_mcp.sync import SyncEngine
from email_mcp.tools.reading import _resolve_email

logger = structlog.get_logger()

# Module-level refs — set during server lifespan
_imap: ImapMutator | None = None
_sync_engine: SyncEngine | None = None
_store: MaildirStore | None = None


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
    message_ids: list[str], folder: str | None = None
) -> dict[str, Any]:
    """Archive multiple emails in one call.

    Args:
        message_ids: List of Message-ID header values to archive
        folder: Current folder hint (optional, speeds up UID lookup)
    """
    logger.info("tool.batch_archive", count=len(message_ids), folder=folder)
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

    sync.request_reindex()
    logger.info("tool.batch_archive.done", succeeded=succeeded, failed=len(errors))
    return {
        "status": "completed",
        "succeeded": succeeded,
        "failed": len(errors),
        "errors": errors,
    }


@mcp.tool(
    annotations={"destructiveHint": False, "title": "Batch Mark Read"}
)
async def batch_mark_read(
    message_ids: list[str], folder: str | None = None
) -> dict[str, Any]:
    r"""Mark multiple emails as read in one call.

    Args:
        message_ids: List of Message-ID header values to mark as read
        folder: Current folder hint (optional)
    """
    logger.info("tool.batch_mark_read", count=len(message_ids), folder=folder)
    imap = _require_imap()

    try:
        succeeded, errors = await imap.batch_add_flags(
            message_ids, r"\Seen", folder=folder
        )
    except (ImapError, Exception) as e:
        logger.error("tool.batch_mark_read.imap_failed", error=str(e))
        return {"error": "imap_error", "detail": str(e)}

    logger.info("tool.batch_mark_read.done", succeeded=succeeded, failed=len(errors))
    return {
        "status": "completed",
        "succeeded": succeeded,
        "failed": len(errors),
        "errors": errors,
    }


@mcp.tool(
    annotations={"destructiveHint": True, "title": "Batch Delete Emails"}
)
async def batch_delete(
    message_ids: list[str],
    confirm: bool = False,
    folder: str | None = None,
) -> dict[str, Any]:
    """Delete multiple emails by moving them to Trash.

    Requires confirm=True to execute. Returns an error if confirm is False.

    Args:
        message_ids: List of Message-ID header values to delete
        confirm: Must be True to proceed with deletion
        folder: Current folder hint (optional)
    """
    logger.info("tool.batch_delete", count=len(message_ids), confirm=confirm)
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

    sync.request_reindex()
    logger.info("tool.batch_delete.done", succeeded=succeeded, failed=len(errors))
    return {
        "status": "completed",
        "succeeded": succeeded,
        "failed": len(errors),
        "errors": errors,
    }
