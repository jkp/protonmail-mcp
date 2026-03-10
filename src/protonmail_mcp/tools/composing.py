"""Composing tools: send, reply, forward using himalaya template workflow."""

import re
from typing import Any

from protonmail_mcp.server import himalaya, mcp


def _set_subject_in_template(template: str, subject: str) -> str:
    """Set or replace the Subject header in a template."""
    return re.sub(r"^Subject:.*$", f"Subject: {subject}", template, count=1, flags=re.MULTILINE)


def _set_cc_in_template(template: str, cc: str) -> str:
    """Add or replace the Cc header in a template."""
    if re.search(r"^Cc:", template, flags=re.MULTILINE):
        return re.sub(r"^Cc:.*$", f"Cc: {cc}", template, count=1, flags=re.MULTILINE)
    # Insert Cc after To header
    return re.sub(r"^(To:.*$)", rf"\1\nCc: {cc}", template, count=1, flags=re.MULTILINE)


def _inject_body_into_template(template: str, body: str) -> str:
    """Inject body text into a himalaya template, preserving headers.

    Templates have headers separated from body by a blank line.
    We insert our body text after the blank line separator.
    """
    parts = template.split("\n\n", 1)
    headers = parts[0]
    existing_body = parts[1] if len(parts) > 1 else ""

    if existing_body.strip():
        return f"{headers}\n\n{body}\n\n{existing_body}"
    return f"{headers}\n\n{body}"


def _set_to_in_template(template: str, to: str) -> str:
    """Set or replace the To header in a template."""
    return re.sub(r"^To:.*$", f"To: {to}", template, count=1, flags=re.MULTILINE)


def _get_header(template: str, name: str) -> str:
    """Extract a header value from a template."""
    match = re.search(rf"^{name}:[ \t]*(.*)$", template, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _ensure_to_from_sender(template: str) -> str:
    """If To is empty, copy From value to To (handles self-reply case)."""
    if _get_header(template, "To"):
        return template
    from_val = _get_header(template, "From")
    if from_val:
        return _set_to_in_template(template, from_val)
    return template


@mcp.tool(annotations={"destructiveHint": False, "title": "Send Email"})
async def send(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Send a new email.

    Args:
        to: Recipient email address
        subject: Email subject
        body: Email body text
        cc: CC recipients (comma-separated)
        account: Optional account/identity to send from
    """
    # Get a blank template with From pre-filled from the account
    result = await himalaya.run_json("template", "write", account=account)
    template = result["content"]
    template = _set_to_in_template(template, to)
    template = _set_subject_in_template(template, subject)
    if cc:
        template = _set_cc_in_template(template, cc)
    template = _inject_body_into_template(template, body)
    await himalaya.run("template", "send", stdin=template, account=account)
    return {"status": "sent", "to": to, "subject": subject}


@mcp.tool(annotations={"destructiveHint": False, "title": "Reply to Email"})
async def reply(
    email_id: str,
    body: str,
    folder: str = "INBOX",
    reply_all: bool = False,
    account: str | None = None,
) -> dict[str, Any]:
    """Reply to an email.

    Args:
        email_id: The email ID/UID to reply to
        body: Reply body text
        folder: Folder containing the email
        reply_all: Whether to reply to all recipients
        account: Optional account/identity to send from
    """
    # Step 1: Get reply template from himalaya
    reply_args = ["template", "reply", email_id, "--folder", folder]
    if reply_all:
        reply_args.append("--all")
    result = await himalaya.run_json(*reply_args, account=account)
    template = result["content"]

    # Step 2: Ensure To is populated (empty for self-replies)
    template = _ensure_to_from_sender(template)

    # Step 3: Inject our body into the template
    edited = _inject_body_into_template(template, body)

    # Step 4: Send the edited template
    await himalaya.run("template", "send", stdin=edited, account=account)
    return {"status": "sent", "in_reply_to": email_id}


@mcp.tool(annotations={"destructiveHint": False, "title": "Forward Email"})
async def forward(
    email_id: str,
    to: str,
    body: str,
    folder: str = "INBOX",
    account: str | None = None,
) -> dict[str, Any]:
    """Forward an email to another recipient.

    Args:
        email_id: The email ID/UID to forward
        to: Recipient email address
        body: Additional body text to prepend
        folder: Folder containing the email
        account: Optional account/identity to send from
    """
    # Step 1: Get forward template
    result = await himalaya.run_json(
        "template", "forward", email_id, "--folder", folder, account=account
    )
    template = result["content"]

    # Step 2: Set the To header and inject body
    edited = _set_to_in_template(template, to)
    edited = _inject_body_into_template(edited, body)

    # Step 3: Send
    await himalaya.run("template", "send", stdin=edited, account=account)
    return {"status": "sent", "forwarded_to": to, "original_id": email_id}
