"""Reply, forward, and new message composition using stdlib email."""

import re
from email.message import EmailMessage

from email_mcp.models import Address


def _strip_re(subject: str) -> str:
    """Strip Re: prefix from a subject line."""
    return re.sub(r"^(Re:\s*)+", "", subject, flags=re.IGNORECASE).strip()


def _strip_fwd(subject: str) -> str:
    """Strip Fwd:/Fw: prefix from a subject line."""
    return re.sub(r"^(Fwd?:\s*)+", "", subject, flags=re.IGNORECASE).strip()


def _build_references(original: EmailMessage) -> str:
    """Build References header from original message."""
    refs = original.get("References", "")
    mid = original.get("Message-ID", "")
    if refs and mid:
        return f"{refs} {mid}"
    return mid or refs


def _quote_body(original: EmailMessage) -> str:
    """Quote the body of the original message for reply."""
    body = original.get_body(preferencelist=("plain",))
    if body is None:
        return ""
    content = body.get_content()
    if not isinstance(content, str):
        return ""
    from_addr = original.get("From", "unknown")
    date = original.get("Date", "")
    header = f"On {date}, {from_addr} wrote:"
    quoted = "\n".join(f"> {line}" for line in content.splitlines())
    return f"\n\n{header}\n{quoted}"


def _format_forwarded(original: EmailMessage) -> str:
    """Format original message for forwarding."""
    body = original.get_body(preferencelist=("plain",))
    content = ""
    if body is not None:
        payload = body.get_content()
        if isinstance(payload, str):
            content = payload

    return (
        "\n\n---------- Forwarded message ----------\n"
        f"From: {original.get('From', '')}\n"
        f"Date: {original.get('Date', '')}\n"
        f"Subject: {original.get('Subject', '')}\n"
        f"To: {original.get('To', '')}\n"
        f"\n{content}"
    )


def build_new(
    from_addr: Address,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
) -> EmailMessage:
    """Build a new email message."""
    msg = EmailMessage()
    msg["From"] = str(from_addr)
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    msg.set_content(body)
    return msg


def build_reply(
    original: EmailMessage,
    body: str,
    from_addr: Address,
    reply_all: bool = False,
) -> EmailMessage:
    """Build a reply message with proper headers."""
    reply = EmailMessage()
    reply["From"] = str(from_addr)
    reply["Subject"] = f"Re: {_strip_re(original.get('Subject', ''))}"

    # Reply-To takes precedence
    reply_to = original.get("Reply-To") or original.get("From", "")
    reply["To"] = reply_to

    if reply_all:
        # Add all original To and Cc, excluding our own address
        to_addrs = original.get("To", "")
        cc_addrs = original.get("Cc", "")
        all_addrs = f"{to_addrs}, {cc_addrs}".strip(", ")
        if all_addrs:
            reply["Cc"] = all_addrs

    reply["In-Reply-To"] = original.get("Message-ID", "")
    refs = _build_references(original)
    if refs:
        reply["References"] = refs

    quoted = _quote_body(original)
    reply.set_content(f"{body}{quoted}")
    return reply


def build_forward(
    original: EmailMessage,
    to: str,
    body: str,
    from_addr: Address,
) -> EmailMessage:
    """Build a forward message with original content and attachments."""
    fwd = EmailMessage()
    fwd["From"] = str(from_addr)
    fwd["To"] = to
    fwd["Subject"] = f"Fwd: {_strip_fwd(original.get('Subject', ''))}"

    refs = _build_references(original)
    if refs:
        fwd["References"] = refs

    forwarded = _format_forwarded(original)
    fwd.set_content(f"{body}{forwarded}")

    # Re-attach original attachments
    for att in original.iter_attachments():
        content = att.get_payload(decode=True)
        if content is None:
            continue
        maintype, _, subtype = att.get_content_type().partition("/")
        fwd.add_attachment(
            content,
            maintype=maintype,
            subtype=subtype,
            filename=att.get_filename(),
        )

    return fwd
