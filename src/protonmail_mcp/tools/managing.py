"""Managing tools: archive, delete, move_email, set_identity."""

from typing import Any

from protonmail_mcp.server import himalaya, mcp


@mcp.tool(annotations={"destructiveHint": False, "title": "Archive Email"})
async def archive(email_id: str, folder: str = "INBOX") -> dict[str, Any]:
    """Archive an email by moving it to the Archive folder.

    Args:
        email_id: The email ID/UID to archive
        folder: Current folder of the email
    """
    await himalaya.run("message", "move", email_id, "--folder", folder, "Archive")
    return {"status": "archived", "email_id": email_id}


@mcp.tool(annotations={"destructiveHint": True, "title": "Delete Email"})
async def delete(email_id: str, folder: str = "INBOX") -> dict[str, Any]:
    """Delete an email.

    Args:
        email_id: The email ID/UID to delete
        folder: Folder containing the email
    """
    await himalaya.run("message", "delete", email_id, "--folder", folder)
    return {"status": "deleted", "email_id": email_id}


@mcp.tool(annotations={"destructiveHint": False, "title": "Move Email"})
async def move_email(email_id: str, from_folder: str, to_folder: str) -> dict[str, Any]:
    """Move an email to a different folder.

    Args:
        email_id: The email ID/UID to move
        from_folder: Current folder
        to_folder: Destination folder
    """
    await himalaya.run("message", "move", email_id, "--folder", from_folder, to_folder)
    return {"status": "moved", "email_id": email_id, "to_folder": to_folder}


@mcp.tool(annotations={"destructiveHint": False, "title": "Set Identity"})
async def set_identity(account: str) -> dict[str, Any]:
    """Set the default sending identity/account for ALL subsequent operations.

    Warning: This changes global state. Prefer using the per-call 'account' parameter
    on send/reply/forward instead. In HTTP mode with concurrent clients, this could
    cause one client's identity to affect another's operations.

    Args:
        account: The himalaya account name to use as default
    """
    himalaya.account = account
    return {"status": "identity_set", "account": account}
