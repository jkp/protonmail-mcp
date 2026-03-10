"""Tests for data models."""

import json

from protonmail_mcp.models import (
    Address,
    Attachment,
    Envelope,
    Folder,
    Message,
    SearchResult,
)


class TestAddress:
    def test_from_dict(self) -> None:
        data = {"name": "Alice", "addr": "alice@example.com"}
        addr = Address.model_validate(data)
        assert addr.name == "Alice"
        assert addr.addr == "alice@example.com"

    def test_display(self) -> None:
        addr = Address(name="Alice", addr="alice@example.com")
        assert str(addr) == "Alice <alice@example.com>"

    def test_display_no_name(self) -> None:
        addr = Address(name="", addr="alice@example.com")
        assert str(addr) == "alice@example.com"


class TestEnvelope:
    def test_parse_from_himalaya_json(self, sample_envelope_json: str) -> None:
        data = json.loads(sample_envelope_json)
        envelopes = [Envelope.model_validate(item) for item in data]
        assert len(envelopes) == 2
        assert envelopes[0].id == "42"
        assert envelopes[0].subject == "Test Subject"
        assert envelopes[0].from_.addr == "alice@example.com"
        assert envelopes[1].id == "43"

    def test_single_address_to_coerced_to_list(self) -> None:
        """Real himalaya returns single address object for `to`, not a list."""
        data = {
            "id": "1556",
            "flags": ["Seen"],
            "from": {"name": "HL", "addr": "hl@example.com"},
            "to": {"name": None, "addr": "jamie@kirkpatrick.email"},
            "subject": "Tax Year End",
            "date": "2026-03-10 08:05+00:00",
            "has_attachment": False,
        }
        env = Envelope.model_validate(data)
        assert len(env.to) == 1
        assert env.to[0].addr == "jamie@kirkpatrick.email"
        assert env.to[0].name is None

    def test_null_name_in_address(self) -> None:
        data = {
            "id": "1",
            "from": {"name": None, "addr": "a@b.com"},
            "to": {"name": None, "addr": "c@d.com"},
            "subject": "",
            "date": "",
        }
        env = Envelope.model_validate(data)
        assert env.from_.name is None
        assert str(env.from_) == "a@b.com"


class TestMessage:
    def test_parse_from_himalaya_json(self, sample_message_json: str) -> None:
        data = json.loads(sample_message_json)
        msg = Message.model_validate(data)
        assert msg.id == "42"
        assert msg.subject == "Test Subject"
        assert msg.from_.addr == "alice@example.com"
        assert msg.text_plain == "Hello, this is a test email."
        assert msg.text_html is not None
        assert len(msg.attachments) == 1
        assert msg.attachments[0].filename == "doc.pdf"

    def test_message_optional_fields(self) -> None:
        data = {
            "id": "1",
            "from": {"name": "", "addr": "x@x.com"},
            "to": [],
            "subject": "Test",
            "date": "2026-03-10T08:00:00Z",
        }
        msg = Message.model_validate(data)
        assert msg.text_plain is None
        assert msg.text_html is None
        assert msg.attachments == []
        assert msg.cc == []
        assert msg.bcc == []


class TestAttachment:
    def test_parse(self) -> None:
        data = {"filename": "doc.pdf", "content-type": "application/pdf", "size": 1024}
        att = Attachment.model_validate(data)
        assert att.filename == "doc.pdf"
        assert att.content_type == "application/pdf"
        assert att.size == 1024


class TestFolder:
    def test_parse_from_himalaya_json(self, sample_folder_json: str) -> None:
        data = json.loads(sample_folder_json)
        folders = [Folder.model_validate(item) for item in data]
        assert len(folders) == 5
        assert folders[0].name == "INBOX"
        assert folders[4].name == "Trash"


class TestSearchResult:
    def test_create(self) -> None:
        result = SearchResult(
            uid="42",
            folder="INBOX",
            subject="Test",
            date="2026-03-10",
            authors="Alice",
        )
        assert result.uid == "42"
        assert result.folder == "INBOX"
        assert result.subject == "Test"
