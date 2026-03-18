"""Tests for email_mcp.composer — reply, forward, new message construction."""

from email.message import EmailMessage

from email_mcp.composer import build_forward, build_new, build_reply
from email_mcp.models import Address


def _make_original(
    from_addr: str = "alice@example.com",
    to_addr: str = "bob@example.com",
    subject: str = "Test Subject",
    body: str = "Original body",
    message_id: str = "<orig@example.com>",
    date: str = "Mon, 10 Mar 2025 12:00:00 +0000",
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = date
    msg.set_content(body)
    return msg


_BOB = Address(name="Bob", addr="bob@example.com")


class TestBuildNew:
    def test_basic_email(self):
        msg = build_new(_BOB, "alice@example.com", "Hello", "Hi Alice!")
        assert msg["To"] == "alice@example.com"
        assert msg["Subject"] == "Hello"
        assert msg["From"] == "Bob <bob@example.com>"

    def test_with_cc(self):
        msg = build_new(_BOB, "alice@example.com", "Hello", "Hi!", cc="charlie@example.com")
        assert msg["Cc"] == "charlie@example.com"

    def test_body_content(self):
        msg = build_new(_BOB, "alice@example.com", "Hello", "Hi Alice!")
        body = msg.get_body(preferencelist=("plain",))
        assert body is not None
        assert "Hi Alice!" in body.get_content()


class TestBuildReply:
    def test_subject_prefix(self):
        original = _make_original(subject="Hello")
        reply = build_reply(original, "Thanks!", _BOB)
        assert reply["Subject"] == "Re: Hello"

    def test_no_double_re(self):
        original = _make_original(subject="Re: Hello")
        reply = build_reply(original, "Thanks!", _BOB)
        assert reply["Subject"] == "Re: Hello"

    def test_in_reply_to(self):
        original = _make_original(message_id="<orig@example.com>")
        reply = build_reply(original, "Thanks!", _BOB)
        assert reply["In-Reply-To"] == "<orig@example.com>"

    def test_references(self):
        original = _make_original(message_id="<orig@example.com>")
        reply = build_reply(original, "Thanks!", _BOB)
        assert "<orig@example.com>" in reply["References"]

    def test_references_chain(self):
        original = _make_original(message_id="<msg2@example.com>")
        original["References"] = "<msg1@example.com>"
        reply = build_reply(original, "Thanks!", _BOB)
        assert "<msg1@example.com>" in reply["References"]
        assert "<msg2@example.com>" in reply["References"]

    def test_to_from_original(self):
        original = _make_original(from_addr="alice@example.com")
        reply = build_reply(original, "Thanks!", _BOB)
        assert reply["To"] == "alice@example.com"

    def test_reply_to_header(self):
        original = _make_original(from_addr="alice@example.com")
        original["Reply-To"] = "reply@example.com"
        reply = build_reply(original, "Thanks!", _BOB)
        assert reply["To"] == "reply@example.com"

    def test_reply_all_cc(self):
        original = _make_original(
            from_addr="alice@example.com",
            to_addr="bob@example.com",
        )
        original["Cc"] = "charlie@example.com"
        reply = build_reply(original, "Thanks!", _BOB, reply_all=True)
        assert reply["Cc"] is not None

    def test_quoted_body(self):
        original = _make_original(body="Original body")
        reply = build_reply(original, "My reply", _BOB)
        body = reply.get_body(preferencelist=("plain",))
        assert body is not None
        content = body.get_content()
        assert "My reply" in content
        assert "> Original body" in content

    def test_from_address(self):
        original = _make_original()
        reply = build_reply(original, "Thanks!", _BOB)
        assert reply["From"] == "Bob <bob@example.com>"


class TestBuildForward:
    def test_subject_prefix(self):
        original = _make_original(subject="Hello")
        fwd = build_forward(original, "charlie@example.com", "FYI", _BOB)
        assert fwd["Subject"] == "Fwd: Hello"

    def test_no_double_fwd(self):
        original = _make_original(subject="Fwd: Hello")
        fwd = build_forward(original, "charlie@example.com", "FYI", _BOB)
        assert fwd["Subject"] == "Fwd: Hello"

    def test_to_recipient(self):
        original = _make_original()
        fwd = build_forward(original, "charlie@example.com", "FYI", _BOB)
        assert fwd["To"] == "charlie@example.com"

    def test_forwarded_body(self):
        original = _make_original(body="Original body", from_addr="alice@example.com")
        fwd = build_forward(original, "charlie@example.com", "FYI", _BOB)
        body = fwd.get_body(preferencelist=("plain",))
        assert body is not None
        content = body.get_content()
        assert "FYI" in content
        assert "Forwarded message" in content
        assert "Original body" in content
        assert "alice@example.com" in content

    def test_reattaches_attachments(self):
        original = EmailMessage()
        original["From"] = "alice@example.com"
        original["To"] = "bob@example.com"
        original["Subject"] = "With attachment"
        original["Message-ID"] = "<orig@example.com>"
        original["Date"] = "Mon, 10 Mar 2025 12:00:00 +0000"
        original.set_content("Body")
        original.add_attachment(
            b"data", maintype="application", subtype="octet-stream", filename="file.bin"
        )

        fwd = build_forward(original, "charlie@example.com", "FYI", _BOB)
        attachments = list(fwd.iter_attachments())
        assert len(attachments) == 1
        assert attachments[0].get_filename() == "file.bin"
