"""Tests for composing tools (send, reply, forward)."""

from unittest.mock import AsyncMock, patch

from protonmail_mcp.tools.composing import (
    _ensure_to_from_sender,
    _get_header,
    forward,
    reply,
    send,
)


class TestSend:
    async def test_gets_template_and_sends(self) -> None:
        template_json = {"content": "From: me@example.com\nTo: \nSubject: \n\n"}
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=template_json)
            mock_himalaya.run = AsyncMock(return_value="Message sent")
            result = await send(
                to="alice@example.com",
                subject="Hello",
                body="Hi Alice!",
            )
            # First call: template write via run_json
            mock_himalaya.run_json.assert_called_once()
            write_call = mock_himalaya.run_json.call_args
            assert "template" in write_call[0]
            assert "write" in write_call[0]
            # Second call: template send with stdin
            mock_himalaya.run.assert_called_once()
            send_call = mock_himalaya.run.call_args
            assert "template" in send_call[0]
            assert "send" in send_call[0]
            stdin = send_call[1]["stdin"]
            assert "To: alice@example.com" in stdin
            assert "Subject: Hello" in stdin
            assert "Hi Alice!" in stdin
            assert result["status"] == "sent"

    async def test_send_with_cc(self) -> None:
        template_json = {"content": "From: me@example.com\nTo: \nSubject: \n\n"}
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=template_json)
            mock_himalaya.run = AsyncMock(return_value="Message sent")
            await send(
                to="alice@example.com",
                subject="Hello",
                body="Hi!",
                cc="bob@example.com",
            )
            stdin = mock_himalaya.run.call_args[1]["stdin"]
            assert "Cc: bob@example.com" in stdin

    async def test_send_with_account_override(self) -> None:
        template_json = {"content": "From: me@example.com\nTo: \nSubject: \n\n"}
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=template_json)
            mock_himalaya.run = AsyncMock(return_value="Message sent")
            await send(
                to="alice@example.com",
                subject="Hello",
                body="Hi!",
                account="travel",
            )
            assert mock_himalaya.run_json.call_args[1]["account"] == "travel"
            assert mock_himalaya.run.call_args[1]["account"] == "travel"


class TestGetHeader:
    def test_extracts_existing_header(self) -> None:
        template = "From: alice@example.com\nTo: bob@example.com\nSubject: Hi\n\n"
        assert _get_header(template, "From") == "alice@example.com"
        assert _get_header(template, "To") == "bob@example.com"
        assert _get_header(template, "Subject") == "Hi"

    def test_returns_empty_for_missing_header(self) -> None:
        template = "From: alice@example.com\nTo: bob@example.com\n\n"
        assert _get_header(template, "Cc") == ""

    def test_returns_empty_for_blank_value(self) -> None:
        template = "From: alice@example.com\nTo: \n\n"
        assert _get_header(template, "To") == ""


class TestEnsureToFromSender:
    def test_noop_when_to_populated(self) -> None:
        template = "From: me@example.com\nTo: alice@example.com\nSubject: Hi\n\n"
        assert _ensure_to_from_sender(template) == template

    def test_copies_from_to_to_when_empty(self) -> None:
        template = "From: me@example.com\nTo: \nSubject: Re: Test\n\n"
        result = _ensure_to_from_sender(template)
        assert "To: me@example.com" in result

    def test_copies_from_with_name_to_to(self) -> None:
        template = "From: Jamie Kirkpatrick <jamie@example.com>\nTo: \nSubject: Re: Test\n\n"
        result = _ensure_to_from_sender(template)
        assert "To: Jamie Kirkpatrick <jamie@example.com>" in result

    def test_noop_when_both_empty(self) -> None:
        template = "From: \nTo: \nSubject: Re: Test\n\n"
        result = _ensure_to_from_sender(template)
        assert "To: \n" in result


class TestReply:
    async def test_gets_template_and_sends(self) -> None:
        template_json = {"content": "From: bob@example.com\nTo: alice@example.com\nSubject: Re: Test\n\n"}
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=template_json)
            mock_himalaya.run = AsyncMock(return_value="Message sent")
            result = await reply(
                email_id="42",
                body="Thanks for the email!",
                folder="INBOX",
            )
            # First call: get template via run_json
            write_call = mock_himalaya.run_json.call_args
            assert "template" in write_call[0]
            assert "reply" in write_call[0]
            assert "42" in write_call[0]
            # Second call: send via run
            send_call = mock_himalaya.run.call_args
            assert "template" in send_call[0]
            assert "send" in send_call[0]
            stdin = send_call[1]["stdin"]
            assert "Thanks for the email!" in stdin
            assert result["status"] == "sent"

    async def test_reply_all(self) -> None:
        template_json = {"content": "From: bob@example.com\nTo: alice@example.com\nSubject: Re: Test\n\n"}
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=template_json)
            mock_himalaya.run = AsyncMock(return_value="Message sent")
            await reply(email_id="42", body="Thanks!", folder="INBOX", reply_all=True)
            write_call = mock_himalaya.run_json.call_args
            assert "--all" in write_call[0]

    async def test_reply_with_account(self) -> None:
        template_json = {"content": "From: bob@example.com\nTo: alice@example.com\nSubject: Re: Test\n\n"}
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=template_json)
            mock_himalaya.run = AsyncMock(return_value="Message sent")
            await reply(email_id="42", body="Thanks!", folder="INBOX", account="work")
            assert mock_himalaya.run_json.call_args[1]["account"] == "work"
            assert mock_himalaya.run.call_args[1]["account"] == "work"

    async def test_self_reply_populates_to_from_from(self) -> None:
        template_json = {"content": "From: me@example.com\nTo: \nSubject: Re: Test\n\n"}
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=template_json)
            mock_himalaya.run = AsyncMock(return_value="Message sent")
            await reply(email_id="42", body="Self-reply", folder="INBOX")
            stdin = mock_himalaya.run.call_args[1]["stdin"]
            assert "To: me@example.com" in stdin


class TestForward:
    async def test_gets_template_edits_and_sends(self) -> None:
        template_json = {"content": "From: bob@example.com\nTo: \nSubject: Fwd: Test\n\n---------- Forwarded message ----------\nOriginal content"}
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=template_json)
            mock_himalaya.run = AsyncMock(return_value="Message sent")
            result = await forward(
                email_id="42",
                to="charlie@example.com",
                body="FYI see below",
                folder="INBOX",
            )
            send_call = mock_himalaya.run.call_args
            stdin = send_call[1]["stdin"]
            assert "charlie@example.com" in stdin
            assert "FYI see below" in stdin
            assert result["status"] == "sent"

    async def test_forward_with_account(self) -> None:
        template_json = {"content": "From: bob@example.com\nTo: \nSubject: Fwd: Test\n\n"}
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=template_json)
            mock_himalaya.run = AsyncMock(return_value="Message sent")
            await forward(
                email_id="42",
                to="charlie@example.com",
                body="FYI",
                folder="INBOX",
                account="travel",
            )
            assert mock_himalaya.run_json.call_args[1]["account"] == "travel"
            assert mock_himalaya.run.call_args[1]["account"] == "travel"
