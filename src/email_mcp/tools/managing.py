"""Managing tools: archive, delete, move_email, mark_read, archive_thread, sync_now.

v4: Mutations via ProtonMail native API. SQLite updated optimistically.
Event loop confirms changes on the next poll.
"""

from typing import Any

import structlog

from email_mcp.db import resolve_message
from email_mcp.proton_api import ProtonAPIError, ProtonClient
from email_mcp.server import db, mcp

logger = structlog.get_logger()

# Module-level refs — set during server lifespan
_api: ProtonClient | None = None
_event_loop: Any = None  # EventLoop instance for triggering manual sync

# ProtonMail system label IDs
_FOLDER_TO_LABEL: dict[str, str] = {
    "INBOX": "0",
    "Drafts": "1",
    "Sent": "2",
    "Trash": "3",
    "Spam": "4",
    "Archive": "6",
}


def _require_api() -> ProtonClient:
    if _api is None:
        raise RuntimeError("ProtonMail API not initialized")
    return _api


def _resolve_pm_id(identifier: int | str) -> str | None:
    """Resolve any identifier (numeric id, message_id, pm_id) to a pm_id."""
    msg = resolve_message(db, identifier)
    return msg.pm_id if msg else None


def _optimistic_update_folder(pm_id: str, folder: str) -> None:
    """Update folder in SQLite immediately (event loop confirms later)."""
    label_id = _FOLDER_TO_LABEL.get(folder, "")
    db.execute(
        "UPDATE messages SET folder = ?, label_ids = ?, updated_at = unixepoch() WHERE pm_id = ?",
        [folder, f'["{label_id}"]', pm_id],
    )
    db.commit()


def _optimistic_update_read(pm_id: str, unread: bool) -> None:
    db.execute(
        "UPDATE messages SET unread = ?, updated_at = unixepoch() WHERE pm_id = ?",
        [int(unread), pm_id],
    )
    db.commit()


@mcp.tool(annotations={"destructiveHint": False, "title": "Archive Email"})
async def archive(
    id: int | str | None = None,
    message_id: str | None = None,
    folder: str | None = None,
) -> dict[str, Any]:
    """Archive an email by moving it to the Archive folder.

    Args:
        id: Numeric message id (from search or list results)
        message_id: Legacy message_id or pm_id string (use id instead)
        folder: Ignored in v4 — folder is derived from SQLite
    """
    id = id or message_id
    if id is None:
        return {"error": "missing_id", "detail": "Provide id or message_id."}
    logger.info("tool.archive", id=id)
    pm_id = _resolve_pm_id(id)
    if pm_id is None:
        return {"error": "not_found", "id": id}

    api = _require_api()
    try:
        await api.label_messages([pm_id], "6")
    except ProtonAPIError as e:
        logger.error("tool.archive.api_failed", pm_id=pm_id, error=str(e))
        return {"error": "api_error", "detail": str(e)}

    _optimistic_update_folder(pm_id, "Archive")
    logger.info("tool.archive.done", pm_id=pm_id)
    return {"status": "archived", "id": id}


@mcp.tool(annotations={"destructiveHint": True, "title": "Delete Email"})
async def delete(
    id: int | str | None = None,
    message_id: str | None = None,
    folder: str | None = None,
) -> dict[str, Any]:
    """Delete an email by moving it to Trash.

    Args:
        id: Numeric message id (from search or list results)
        message_id: Legacy message_id or pm_id string (use id instead)
        folder: Ignored in v4
    """
    id = id or message_id
    if id is None:
        return {"error": "missing_id", "detail": "Provide id or message_id."}
    logger.info("tool.delete", id=id)
    pm_id = _resolve_pm_id(id)
    if pm_id is None:
        return {"error": "not_found", "id": id}

    api = _require_api()
    try:
        await api.label_messages([pm_id], "3")
    except ProtonAPIError as e:
        logger.error("tool.delete.api_failed", pm_id=pm_id, error=str(e))
        return {"error": "api_error", "detail": str(e)}

    _optimistic_update_folder(pm_id, "Trash")
    logger.info("tool.delete.done", pm_id=pm_id)
    return {"status": "deleted", "id": id}


@mcp.tool(annotations={"destructiveHint": False, "title": "Move Email"})
async def move_email(
    id: int | str | None = None,
    to_folder: str = "",
    message_id: str | None = None,
    from_folder: str | None = None,
) -> dict[str, Any]:
    """Move an email to a different folder.

    Supported folders: INBOX, Archive, Trash, Spam, Sent, Drafts

    Args:
        id: Numeric message id (from search or list results)
        to_folder: Destination folder name
        message_id: Legacy message_id or pm_id string (use id instead)
        from_folder: Ignored in v4
    """
    id = id or message_id
    if id is None:
        return {"error": "missing_id", "detail": "Provide id or message_id."}
    logger.info("tool.move_email", id=id, to_folder=to_folder)

    label_id = _FOLDER_TO_LABEL.get(to_folder)
    if label_id is None:
        return {
            "error": "unknown_folder",
            "folder": to_folder,
            "valid_folders": list(_FOLDER_TO_LABEL.keys()),
        }

    pm_id = _resolve_pm_id(id)
    if pm_id is None:
        return {"error": "not_found", "id": id}

    api = _require_api()
    try:
        await api.label_messages([pm_id], label_id)
    except ProtonAPIError as e:
        logger.error("tool.move_email.api_failed", pm_id=pm_id, error=str(e))
        return {"error": "api_error", "detail": str(e)}

    _optimistic_update_folder(pm_id, to_folder)
    logger.info("tool.move_email.done", pm_id=pm_id, to_folder=to_folder)
    return {"status": "moved", "id": id, "to_folder": to_folder}


@mcp.tool(annotations={"destructiveHint": False, "title": "Mark Email Read"})
async def mark_read(
    id: int | str | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Mark an email as read.

    Args:
        id: Numeric message id (from search or list results)
        message_id: Legacy message_id or pm_id string (use id instead)
    """
    id = id or message_id
    if id is None:
        return {"error": "missing_id", "detail": "Provide id or message_id."}
    logger.info("tool.mark_read", id=id)
    pm_id = _resolve_pm_id(id)
    if pm_id is None:
        return {"error": "not_found", "id": id}

    api = _require_api()
    try:
        await api.mark_read([pm_id])
    except ProtonAPIError as e:
        return {"error": "api_error", "detail": str(e)}

    _optimistic_update_read(pm_id, unread=False)
    return {"status": "ok", "id": id}


@mcp.tool(annotations={"destructiveHint": False, "title": "Archive Thread"})
async def archive_thread(
    id: int | str | None = None,
    message_id: str | None = None,
    mark_as_read: bool = True,
) -> dict[str, Any]:
    """Archive an email thread. Looks up all messages sharing the same
    conversation by subject prefix and archives them via the ProtonMail API.

    Args:
        id: Numeric message id (from search or list results)
        message_id: Legacy message_id or pm_id string (use id instead)
        mark_as_read: Mark all messages as read before archiving (default: True)
    """
    id = id or message_id
    if id is None:
        return {"error": "missing_id", "detail": "Provide id or message_id."}
    logger.info("tool.archive_thread", id=id)

    # Find the anchor message
    msg = resolve_message(db, id)
    if msg is None:
        return {"error": f"Message not found: {id}"}
    row = (msg.pm_id, msg.subject, msg.folder)

    anchor_pm_id, subject, anchor_folder = row[0], row[1], row[2]

    # Find all non-archived messages with the same subject (simple thread heuristic)
    # A proper implementation would use ConversationID from the ProtonMail API
    base_subject = (subject or "").removeprefix("Re: ").removeprefix("Fwd: ").strip()
    thread_rows = db.execute(
        "SELECT pm_id, folder FROM messages"
        " WHERE (subject = ? OR subject = ? OR subject = ?)"
        " AND folder != 'Archive'",
        [base_subject, f"Re: {base_subject}", f"Fwd: {base_subject}"],
    ).fetchall()

    # If no other messages found, just use the anchor
    if not thread_rows:
        if anchor_folder == "Archive":
            return {"status": "archived", "archived": 0, "skipped": 1, "total": 1}
        thread_rows = [(anchor_pm_id, anchor_folder)]

    api = _require_api()
    to_archive = [r[0] for r in thread_rows if r[1] != "Archive"]
    skipped = len([r for r in thread_rows if r[1] == "Archive"])

    if not to_archive and anchor_folder == "Archive":
        return {"status": "archived", "archived": 0, "skipped": 1, "total": 1}

    try:
        if mark_as_read and to_archive:
            await api.mark_read(to_archive)
        if to_archive:
            await api.label_messages(to_archive, "6")
            for pm_id in to_archive:
                _optimistic_update_folder(pm_id, "Archive")
    except ProtonAPIError as e:
        return {"error": "api_error", "detail": str(e)}

    logger.info(
        "tool.archive_thread.done",
        archived=len(to_archive),
        skipped=skipped,
        total=len(to_archive) + skipped,
    )
    return {
        "status": "archived",
        "archived": len(to_archive),
        "skipped": skipped,
        "total": len(to_archive) + skipped,
    }


@mcp.tool(annotations={"destructiveHint": False, "title": "Sync Now"})
async def sync_now() -> dict[str, Any]:
    """Trigger an immediate event poll and report sync state.

    Polls the ProtonMail event stream for new messages, then returns
    current database statistics.
    """
    logger.info("tool.sync_now")

    # Trigger an immediate event poll if event loop is available
    synced = False
    if _event_loop is not None:
        try:
            await _event_loop.poll_once()
            synced = True
        except Exception:
            logger.warning("tool.sync_now.poll_failed", exc_info=True)

    row = db.execute(
        "SELECT COUNT(*), SUM(CASE WHEN unread=1 THEN 1 ELSE 0 END) FROM messages"
    ).fetchone()
    total = row[0] or 0
    unread = row[1] or 0

    unindexed = db.execute("SELECT COUNT(*) FROM messages WHERE body_indexed = 0").fetchone()[0]
    decrypt_failed = db.execute("SELECT COUNT(*) FROM messages WHERE body_indexed = -1").fetchone()[
        0
    ]

    last_event = db.sync_state.get("last_event_id")
    initial_done = db.sync_state.get("initial_sync_done") == "1"

    return {
        "status": "ok",
        "synced": synced,
        "message_count": total,
        "unread_count": unread,
        "bodies_pending_index": unindexed,
        "bodies_decrypt_failed": decrypt_failed,
        "last_event_id": last_event,
        "initial_sync_done": initial_done,
    }
