"""Managing tools: archive, delete, move_email, archive_thread, sync_now.

v3: IMAP-first mutations with optimistic local moves.
"""

from typing import Any

import structlog

from email_mcp.imap import ImapError, ImapMutator
from email_mcp.search import NotmuchSearcher
from email_mcp.server import mcp
from email_mcp.store import MaildirStore
from email_mcp.sync import SyncEngine

logger = structlog.get_logger()

# Module-level refs — set during server lifespan
_imap: ImapMutator | None = None
_sync_engine: SyncEngine | None = None
_store: MaildirStore | None = None
_searcher: NotmuchSearcher | None = None


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


@mcp.tool(annotations={"destructiveHint": False, "title": "Archive Email"})
async def archive(message_id: str, folder: str | None = None) -> dict[str, Any]:
    """Archive an email by moving it to the Archive folder.

    Args:
        message_id: The Message-ID of the email to archive
        folder: Current folder of the email (optional hint)
    """
    logger.info("tool.archive", message_id=message_id, folder=folder)
    imap = _require_imap()
    store = _require_store()
    sync = _require_sync()

    try:
        await imap.archive(message_id, from_folder=folder)
    except ImapError as e:
        logger.error("tool.archive.imap_failed", message_id=message_id, error=str(e))
        return {"error": "imap_error", "detail": str(e)}

    # Optimistic local move (best-effort)
    if not store.optimistic_move(message_id, "Archive", folder):
        logger.debug("tool.archive.optimistic_move_failed", message_id=message_id)

    sync.request_reindex()
    logger.info("tool.archive.done", message_id=message_id)
    return {"status": "archived", "message_id": message_id}


@mcp.tool(annotations={"destructiveHint": True, "title": "Delete Email"})
async def delete(message_id: str, folder: str | None = None) -> dict[str, Any]:
    """Delete an email by moving it to Trash.

    Args:
        message_id: The Message-ID of the email to delete
        folder: Current folder of the email (optional hint)
    """
    logger.info("tool.delete", message_id=message_id, folder=folder)
    imap = _require_imap()
    store = _require_store()
    sync = _require_sync()

    try:
        await imap.delete(message_id, from_folder=folder)
    except ImapError as e:
        logger.error("tool.delete.imap_failed", message_id=message_id, error=str(e))
        return {"error": "imap_error", "detail": str(e)}

    if not store.optimistic_move(message_id, "Trash", folder):
        logger.debug("tool.delete.optimistic_move_failed", message_id=message_id)

    sync.request_reindex()
    logger.info("tool.delete.done", message_id=message_id)
    return {"status": "deleted", "message_id": message_id}


@mcp.tool(annotations={"destructiveHint": False, "title": "Move Email"})
async def move_email(
    message_id: str, to_folder: str, from_folder: str | None = None
) -> dict[str, Any]:
    """Move an email to a different folder.

    Args:
        message_id: The Message-ID of the email to move
        to_folder: Destination folder
        from_folder: Current folder (optional hint)
    """
    logger.info("tool.move_email", message_id=message_id, to_folder=to_folder)
    imap = _require_imap()
    store = _require_store()
    sync = _require_sync()

    try:
        await imap.move(message_id, to_folder, from_folder=from_folder)
    except ImapError as e:
        logger.error("tool.move_email.imap_failed", message_id=message_id, error=str(e))
        return {"error": "imap_error", "detail": str(e)}

    if not store.optimistic_move(message_id, to_folder, from_folder):
        logger.debug("tool.move_email.optimistic_move_failed", message_id=message_id)

    sync.request_reindex()
    logger.info("tool.move_email.done", message_id=message_id, to_folder=to_folder)
    return {"status": "moved", "message_id": message_id, "to_folder": to_folder}


@mcp.tool(annotations={"destructiveHint": False, "title": "Archive Thread"})
async def archive_thread(
    message_id: str,
    mark_as_read: bool = True,
) -> dict[str, Any]:
    """Archive an entire email thread. Finds all messages in the thread
    via notmuch, archives each via IMAP, and optimistically moves local files.

    Args:
        message_id: The Message-ID of any email in the thread
        mark_as_read: Mark all messages as read before archiving (default: True)
    """
    logger.info("tool.archive_thread", message_id=message_id, mark_as_read=mark_as_read)
    imap = _require_imap()
    store = _require_store()
    sync = _require_sync()

    if _searcher is None:
        return {"error": "Search not initialized"}

    messages = await _searcher.find_thread_messages(message_id)
    if not messages:
        return {"error": f"Thread not found for: {message_id}"}

    archived = 0
    skipped = 0
    failed = 0
    for msg in messages:
        mid = msg["message_id"]
        path = msg.get("path", "")

        # Derive folder from notmuch path
        folder = None
        if path and store.root:
            try:
                from pathlib import Path

                relative = Path(path).relative_to(store.root)
                parts = relative.parts
                if len(parts) >= 3:
                    folder = "/".join(parts[:-2])
            except ValueError:
                pass

        # Skip messages already in Archive
        if folder == "Archive":
            if mark_as_read:
                try:
                    await imap.add_flags(mid, r"\Seen", folder="Archive")
                except ImapError:
                    pass
            skipped += 1
            continue

        try:
            if mark_as_read:
                try:
                    await imap.add_flags(mid, r"\Seen", folder=folder)
                except ImapError:
                    pass  # Non-fatal
            await imap.archive(mid, from_folder=folder)
            store.optimistic_move(mid, "Archive", folder)
            archived += 1
        except ImapError as e:
            logger.warning("tool.archive_thread.msg_failed", message_id=mid, error=str(e))
            failed += 1

    sync.request_reindex()
    logger.info(
        "tool.archive_thread.done",
        message_id=message_id,
        archived=archived,
        skipped=skipped,
        failed=failed,
        total=len(messages),
    )
    return {
        "status": "archived",
        "archived": archived,
        "skipped": skipped,
        "failed": failed,
        "total": len(messages),
    }


@mcp.tool(annotations={"destructiveHint": False, "title": "Sync Now"})
async def sync_now() -> dict[str, Any]:
    """Trigger an immediate sync (mbsync + notmuch reindex).

    Pushes local Maildir changes to IMAP and pulls new mail.
    """
    logger.info("tool.sync_now")
    sync = _require_sync()
    try:
        await sync.sync()
    except Exception as e:
        return {"status": "error", "error": str(e)}
    status = sync.status
    return {
        "status": "synced",
        "last_sync": status.last_sync.isoformat() if status.last_sync else None,
        "message_count": status.message_count,
    }
