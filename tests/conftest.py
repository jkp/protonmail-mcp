"""Shared test fixtures for email-mcp tests."""

from email.message import EmailMessage
from pathlib import Path

import pytest

from email_mcp.store import MaildirStore


def _make_email(
    message_id: str = "<test@example.com>",
    from_addr: str = "alice@example.com",
    to_addr: str = "bob@example.com",
    subject: str = "Test Subject",
    body: str = "Hello, world!",
    cc: str = "",
    date: str = "Mon, 10 Mar 2025 12:00:00 +0000",
    html_body: str = "",
    attachments: list[tuple[str, str, bytes]] | None = None,
) -> bytes:
    """Build a raw email message as bytes."""
    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = date
    if cc:
        msg["Cc"] = cc

    if html_body and attachments:
        # multipart/mixed with multipart/alternative + attachments
        msg.set_content(body)
        msg.add_alternative(html_body, subtype="html")
        for filename, content_type, data in attachments:
            maintype, _, subtype = content_type.partition("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    elif html_body:
        # multipart/alternative: plain + html
        msg.set_content(body)
        msg.add_alternative(html_body, subtype="html")
    elif attachments:
        # multipart/mixed: text + attachments
        msg.set_content(body)
        for filename, content_type, data in attachments:
            maintype, _, subtype = content_type.partition("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    else:
        msg.set_content(body)

    return msg.as_bytes()


def _populate_maildir(
    root: Path,
    folder: str,
    messages: list[tuple[str, bytes, str]],
) -> list[Path]:
    """Write messages into a Maildir folder.

    Args:
        root: Maildir root
        folder: Folder name
        messages: List of (filename, raw_bytes, subdir) tuples

    Returns:
        List of written file paths
    """
    paths = []
    for filename, raw_bytes, subdir in messages:
        dir_path = root / folder / subdir
        dir_path.mkdir(parents=True, exist_ok=True)
        path = dir_path / filename
        path.write_bytes(raw_bytes)
        paths.append(path)
    return paths


@pytest.fixture
def maildir(tmp_path: Path) -> Path:
    """Create a temporary Maildir with test messages."""
    root = tmp_path / "mail"

    # Create INBOX with messages
    msg1 = _make_email(
        message_id="<msg1@example.com>",
        from_addr="Alice Smith <alice@example.com>",
        to_addr="Bob Jones <bob@example.com>",
        subject="Hello Bob",
        body="Hey there!",
        date="Mon, 10 Mar 2025 12:00:00 +0000",
    )
    msg2 = _make_email(
        message_id="<msg2@example.com>",
        from_addr="Charlie <charlie@example.com>",
        to_addr="bob@example.com",
        subject="Meeting Tomorrow",
        body="See you at 3pm.",
        cc="alice@example.com",
        date="Tue, 11 Mar 2025 09:30:00 +0000",
    )
    msg3_html = _make_email(
        message_id="<msg3@example.com>",
        from_addr="newsletter@example.com",
        to_addr="bob@example.com",
        subject="Weekly Update",
        body="Plain text version",
        html_body="<h1>Weekly Update</h1><p>HTML version</p>",
        date="Wed, 12 Mar 2025 08:00:00 +0000",
    )
    msg4_attachment = _make_email(
        message_id="<msg4@example.com>",
        from_addr="alice@example.com",
        to_addr="bob@example.com",
        subject="Report Attached",
        body="Please find the report attached.",
        attachments=[("report.txt", "text/plain", b"Report content here")],
        date="Thu, 13 Mar 2025 14:00:00 +0000",
    )

    _populate_maildir(
        root,
        "INBOX",
        [
            ("1710072000.msg1.localhost:2,S", msg1, "cur"),
            ("1710147000.msg2.localhost:2,", msg2, "cur"),
            ("1710228000.msg3.localhost:2,S", msg3_html, "cur"),
            ("1710338400.msg4.localhost:2,S", msg4_attachment, "cur"),
        ],
    )

    # Create Sent folder
    sent_msg = _make_email(
        message_id="<sent1@example.com>",
        from_addr="bob@example.com",
        to_addr="alice@example.com",
        subject="Re: Hello Bob",
        body="Hey Alice!",
        date="Mon, 10 Mar 2025 13:00:00 +0000",
    )
    _populate_maildir(
        root,
        "Sent",
        [
            ("1710075600.sent1.localhost:2,S", sent_msg, "cur"),
        ],
    )

    # Create Archive folder (empty)
    (root / "Archive" / "cur").mkdir(parents=True)
    (root / "Archive" / "new").mkdir(parents=True)
    (root / "Archive" / "tmp").mkdir(parents=True)

    # Create Trash folder (empty)
    (root / "Trash" / "cur").mkdir(parents=True)
    (root / "Trash" / "new").mkdir(parents=True)
    (root / "Trash" / "tmp").mkdir(parents=True)

    # Create new/ and tmp/ for INBOX
    (root / "INBOX" / "new").mkdir(parents=True, exist_ok=True)
    (root / "INBOX" / "tmp").mkdir(parents=True, exist_ok=True)

    # Create new/ and tmp/ for Sent
    (root / "Sent" / "new").mkdir(parents=True, exist_ok=True)
    (root / "Sent" / "tmp").mkdir(parents=True, exist_ok=True)

    return root


@pytest.fixture
def store(maildir: Path) -> MaildirStore:
    """Create a MaildirStore against the test Maildir."""
    return MaildirStore(maildir)
