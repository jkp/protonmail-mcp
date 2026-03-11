"""Managing tools: archive, delete, move_email, sync_now, sync_status."""

from typing import Any

import structlog

from email_mcp.server import mcp, store

logger = structlog.get_logger()


@mcp.tool(annotations={"destructiveHint": False, "title": "Archive Email"})
async def archive(message_id: str, folder: str | None = None) -> dict[str, Any]:
    """Archive an email by moving it to the Archive folder.

    Args:
        message_id: The Message-ID of the email to archive
        folder: Current folder of the email (optional hint)
    """
    logger.info("tool.archive", message_id=message_id, folder=folder)
    success = store.archive_email(message_id, folder)
    if not success:
        return {"error": f"Email not found: {message_id}"}
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
    success = store.delete_email(message_id, folder)
    if not success:
        return {"error": f"Email not found: {message_id}"}
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
    success = store.move_email(message_id, to_folder, from_folder)
    if not success:
        return {"error": f"Email not found: {message_id}"}
    logger.info("tool.move_email.done", message_id=message_id, to_folder=to_folder)
    return {"status": "moved", "message_id": message_id, "to_folder": to_folder}
