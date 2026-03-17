"""Tests for email_mcp.models."""

from datetime import UTC, datetime

from email_mcp.models import Address, Attachment, Email, Folder, SearchResult, SyncStatus


def test_address_str_with_name():
    addr = Address(name="Alice", addr="alice@example.com")
    assert str(addr) == "Alice <alice@example.com>"


def test_address_str_without_name():
    addr = Address(addr="alice@example.com")
    assert str(addr) == "alice@example.com"


def test_attachment():
    att = Attachment(filename="report.pdf", content_type="application/pdf", size=1024)
    assert att.filename == "report.pdf"
    assert att.size == 1024


def test_email_defaults():
    email = Email(message_id="<test@example.com>")
    assert email.folder == ""
    assert email.to == []
    assert email.attachments == []
    assert email.tags == set()


def test_folder():
    folder = Folder(name="INBOX", count=42, unread=3)
    assert folder.name == "INBOX"
    assert folder.count == 42


def test_search_result():
    result = SearchResult(
        message_id="<test@example.com>",
        folders=["INBOX"],
        subject="Test",
        tags={"unread", "inbox"},
    )
    assert result.message_id == "<test@example.com>"
    assert "unread" in result.tags
    assert result.folders == ["INBOX"]


def test_sync_status_defaults():
    status = SyncStatus()
    assert status.state == "initializing"
    assert status.last_sync is None
    assert status.message_count == 0


def test_sync_status_ready():
    now = datetime.now(UTC)
    status = SyncStatus(state="ready", last_sync=now, message_count=100)
    assert status.state == "ready"
    assert status.last_sync == now
