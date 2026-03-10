"""Composing tools: send, reply, forward using himalaya template workflow."""

import re
from typing import Any

from protonmail_mcp.server import himalaya, mcp


def _build_template(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
) -> str:
    """Build an RFC2822-ish template for himalaya template send."""
    headers = [
        f"To: {to}",
        f"Subject: {subject}",
    ]
    if cc:
        headers.append(f"Cc: {cc}")
    return "\n".join(headers) + "\n\n" + body


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
    template = _build_template(to=to, subject=subject, body=body, cc=cc)
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
    template = await himalaya.run(*reply_args, account=account)

    # Step 2: Inject our body into the template
    edited = _inject_body_into_template(template, body)

    # Step 3: Send the edited template
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
    template = await himalaya.run(
        "template", "forward", email_id, "--folder", folder, account=account
    )

    # Step 2: Set the To header and inject body
    edited = _set_to_in_template(template, to)
    edited = _inject_body_into_template(edited, body)

    # Step 3: Send
    await himalaya.run("template", "send", stdin=edited, account=account)
    return {"status": "sent", "forwarded_to": to, "original_id": email_id}
