"""Managing tools: archive, delete, move_email, mark_read, archive_thread, sync_now.

v4: Mutations via ProtonMail native API. SQLite updated optimistically.
Event loop confirms changes on the next poll.
"""

from typing import Any

import structlog

from email_mcp.proton_api import ProtonAPIError, ProtonClient
from email_mcp.server import db, mcp

logger = structlog.get_logger()

# Module-level ref — set during server lifespan
_api: ProtonClient | None = None

# ProtonMail system label IDs
_FOLDER_TO_LABEL: dict[str, str] = {
    "INBOX":   "0",
    "Drafts":  "1",
    "Sent":    "2",
    "Trash":   "3",
    "Spam":    "4",
    "Archive": "6",
}


def _require_api() -> ProtonClient:
    if _api is None:
        raise RuntimeError("ProtonMail API not initialized")
    return _api


def _resolve_pm_id(message_id: str) -> str | None:
    """Look up pm_id by RFC 2822 Message-ID or pm_id directly."""
    row = db.execute(
        "SELECT pm_id FROM messages WHERE message_id = ? OR pm_id = ?",
        [message_id, message_id],
    ).fetchone()
    return row[0] if row else None


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
async def archive(message_id: str, folder: str | None = None) -> dict[str, Any]:
    """Archive an email by moving it to the Archive folder.

    Args:
        message_id: The Message-ID (or pm_id) of the email to archive
        folder: Ignored in v4 — folder is derived from SQLite
    """
    logger.info("tool.archive", message_id=message_id)
    pm_id = _resolve_pm_id(message_id)
    if pm_id is None:
        return {"error": "not_found", "message_id": message_id}

    api = _require_api()
    try:
        await api.label_messages([pm_id], "6")
    except ProtonAPIError as e:
        logger.error("tool.archive.api_failed", pm_id=pm_id, error=str(e))
        return {"error": "api_error", "detail": str(e)}

    _optimistic_update_folder(pm_id, "Archive")
    logger.info("tool.archive.done", pm_id=pm_id)
    return {"status": "archived", "message_id": message_id, "pm_id": pm_id}


@mcp.tool(annotations={"destructiveHint": True, "title": "Delete Email"})
async def delete(message_id: str, folder: str | None = None) -> dict[str, Any]:
    """Delete an email by moving it to Trash.

    Args:
        message_id: The Message-ID (or pm_id) of the email to delete
        folder: Ignored in v4
    """
    logger.info("tool.delete", message_id=message_id)
    pm_id = _resolve_pm_id(message_id)
    if pm_id is None:
        return {"error": "not_found", "message_id": message_id}

    api = _require_api()
    try:
        await api.label_messages([pm_id], "3")
    except ProtonAPIError as e:
        logger.error("tool.delete.api_failed", pm_id=pm_id, error=str(e))
        return {"error": "api_error", "detail": str(e)}

    _optimistic_update_folder(pm_id, "Trash")
    logger.info("tool.delete.done", pm_id=pm_id)
    return {"status": "deleted", "message_id": message_id, "pm_id": pm_id}


@mcp.tool(annotations={"destructiveHint": False, "title": "Move Email"})
async def move_email(
    message_id: str, to_folder: str, from_folder: str | None = None
) -> dict[str, Any]:
    """Move an email to a different folder.

    Supported folders: INBOX, Archive, Trash, Spam, Sent, Drafts

    Args:
        message_id: The Message-ID (or pm_id) of the email to move
        to_folder: Destination folder name
        from_folder: Ignored in v4
    """
    logger.info("tool.move_email", message_id=message_id, to_folder=to_folder)

    label_id = _FOLDER_TO_LABEL.get(to_folder)
    if label_id is None:
        return {"error": "unknown_folder", "folder": to_folder,
                "valid_folders": list(_FOLDER_TO_LABEL.keys())}

    pm_id = _resolve_pm_id(message_id)
    if pm_id is None:
        return {"error": "not_found", "message_id": message_id}

    api = _require_api()
    try:
        await api.label_messages([pm_id], label_id)
    except ProtonAPIError as e:
        logger.error("tool.move_email.api_failed", pm_id=pm_id, error=str(e))
        return {"error": "api_error", "detail": str(e)}

    _optimistic_update_folder(pm_id, to_folder)
    logger.info("tool.move_email.done", pm_id=pm_id, to_folder=to_folder)
    return {"status": "moved", "message_id": message_id, "pm_id": pm_id, "to_folder": to_folder}


@mcp.tool(annotations={"destructiveHint": False, "title": "Mark Email Read"})
async def mark_read(message_id: str) -> dict[str, Any]:
    """Mark an email as read.

    Args:
        message_id: The Message-ID (or pm_id) of the email
    """
    logger.info("tool.mark_read", message_id=message_id)
    pm_id = _resolve_pm_id(message_id)
    if pm_id is None:
        return {"error": "not_found", "message_id": message_id}

    api = _require_api()
    try:
        await api.mark_read([pm_id])
    except ProtonAPIError as e:
        return {"error": "api_error", "detail": str(e)}

    _optimistic_update_read(pm_id, unread=False)
    return {"status": "ok", "message_id": message_id, "pm_id": pm_id}


@mcp.tool(annotations={"destructiveHint": False, "title": "Archive Thread"})
async def archive_thread(
    message_id: str,
    mark_as_read: bool = True,
) -> dict[str, Any]:
    """Archive an email thread. Looks up all messages sharing the same
    conversation by subject prefix and archives them via the ProtonMail API.

    Args:
        message_id: The Message-ID (or pm_id) of any email in the thread
        mark_as_read: Mark all messages as read before archiving (default: True)
    """
    logger.info("tool.archive_thread", message_id=message_id)

    # Find the anchor message
    row = db.execute(
        "SELECT pm_id, subject, folder FROM messages WHERE message_id = ? OR pm_id = ?",
        [message_id, message_id],
    ).fetchone()
    if row is None:
        return {"error": f"Message not found: {message_id}"}

    anchor_pm_id, subject, anchor_folder = row[0], row[1], row[2]

    # Find all non-archived messages with the same subject (simple thread heuristic)
    # A proper implementation would use ConversationID from the ProtonMail API
    base_subject = (subject or "").removeprefix("Re: ").removeprefix("Fwd: ").strip()
    thread_rows = db.execute(
        "SELECT pm_id, folder FROM messages WHERE (subject = ? OR subject = ? OR subject = ?) AND folder != 'Archive'",
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
    """Report current sync state from the local SQLite database.

    In v4, sync is event-driven (ProtonMail event loop). This tool returns
    current database statistics rather than triggering a manual sync.
    """
    logger.info("tool.sync_now")

    row = db.execute(
        "SELECT COUNT(*), SUM(CASE WHEN unread=1 THEN 1 ELSE 0 END) FROM messages"
    ).fetchone()
    total = row[0] or 0
    unread = row[1] or 0

    unindexed = db.execute(
        "SELECT COUNT(*) FROM messages WHERE body_indexed = 0"
    ).fetchone()[0]

    last_event = db.sync_state.get("last_event_id")
    initial_done = db.sync_state.get("initial_sync_done") == "1"

    return {
        "status": "ok",
        "message_count": total,
        "unread_count": unread,
        "bodies_pending_index": unindexed,
        "last_event_id": last_event,
        "initial_sync_done": initial_done,
    }
