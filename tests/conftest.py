"""Shared test fixtures for protonmail-mcp tests."""

import pytest


@pytest.fixture
def sample_envelope_json() -> str:
    """Sample himalaya envelope list JSON output (matches real format)."""
    return """[
  {
    "id": "42",
    "flags": ["Seen"],
    "from": {"name": "Alice", "addr": "alice@example.com"},
    "to": {"name": null, "addr": "bob@example.com"},
    "subject": "Test Subject",
    "date": "2026-03-10 08:00+00:00",
    "has_attachment": false
  },
  {
    "id": "43",
    "flags": [],
    "from": {"name": "Charlie", "addr": "charlie@example.com"},
    "to": {"name": null, "addr": "bob@example.com"},
    "subject": "Another Subject",
    "date": "2026-03-10 09:00+00:00",
    "has_attachment": true
  }
]"""


@pytest.fixture
def sample_message_json() -> str:
    """Sample himalaya message read JSON output."""
    return """{
  "id": "42",
  "from": {"name": "Alice", "addr": "alice@example.com"},
  "to": [{"name": "Bob", "addr": "bob@example.com"}],
  "cc": [],
  "bcc": [],
  "subject": "Test Subject",
  "date": "2026-03-10T08:00:00Z",
  "text/plain": "Hello, this is a test email.",
  "text/html": "<html><body><p>Hello, this is a <b>test</b> email.</p></body></html>",
  "attachments": [
    {"filename": "doc.pdf", "content-type": "application/pdf", "size": 1024}
  ]
}"""


@pytest.fixture
def sample_folder_json() -> str:
    """Sample himalaya folder list JSON output."""
    return """[
  {"name": "INBOX", "desc": ""},
  {"name": "Sent", "desc": ""},
  {"name": "Drafts", "desc": ""},
  {"name": "Archive", "desc": ""},
  {"name": "Trash", "desc": ""}
]"""


@pytest.fixture
def sample_template() -> str:
    """Sample himalaya template output for reply."""
    return """From: bob@example.com
To: alice@example.com
Subject: Re: Test Subject
In-Reply-To: <msg-id@example.com>
Date: Mon, 10 Mar 2026 08:00:00 +0000

"""


@pytest.fixture
def sample_maildir_paths() -> list[str]:
    """Sample notmuch search --output=files paths with mbsync UID scheme."""
    return [
        "/home/user/mail/INBOX/cur/1709020800.M123456P1234.hostname,S=5678,U=42:2,S",
        "/home/user/mail/INBOX/cur/1709020900.M234567P2345.hostname,S=6789,U=43:2,S",
        "/home/user/mail/Sent/cur/1709021000.M345678P3456.hostname,S=7890,U=100:2,S",
        "/home/user/mail/Work/Projects/cur/1709021100.M456789P4567.hostname,S=8901,U=200:2,S",
    ]


@pytest.fixture
def sample_notmuch_search_json() -> str:
    """Sample notmuch search --format=json output."""
    return """[
  {
    "thread": "0000000000000001",
    "timestamp": 1709020800,
    "date_relative": "today",
    "matched": 1,
    "total": 1,
    "authors": "Alice",
    "subject": "Test Subject",
    "query": ["id:msg1@example.com", null],
    "tags": ["inbox", "unread"]
  },
  {
    "thread": "0000000000000002",
    "timestamp": 1709020900,
    "date_relative": "today",
    "matched": 1,
    "total": 1,
    "authors": "Charlie",
    "subject": "Another Subject",
    "query": ["id:msg2@example.com", null],
    "tags": ["inbox"]
  }
]"""
