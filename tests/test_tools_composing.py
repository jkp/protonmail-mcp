"""Tests for composing tools (send, reply, forward)."""

from unittest.mock import AsyncMock, patch

from protonmail_mcp.tools.composing import forward, reply, send


class TestSend:
    async def test_builds_template_and_sends(self) -> None:
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run = AsyncMock(return_value="Message sent")
            result = await send(
                to="alice@example.com",
                subject="Hello",
                body="Hi Alice!",
            )
            # Should call template send with stdin
            mock_himalaya.run.assert_called_once()
            call_args = mock_himalaya.run.call_args
            assert "template" in call_args[0]
            assert "send" in call_args[0]
            # Check stdin contains the template
            stdin = call_args[1]["stdin"]
            assert "To: alice@example.com" in stdin
            assert "Subject: Hello" in stdin
            assert "Hi Alice!" in stdin
            assert result["status"] == "sent"

    async def test_send_with_cc(self) -> None:
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
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
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run = AsyncMock(return_value="Message sent")
            await send(
                to="alice@example.com",
                subject="Hello",
                body="Hi!",
                account="travel",
            )
            assert mock_himalaya.run.call_args[1]["account"] == "travel"


class TestReply:
    async def test_gets_template_and_sends(self) -> None:
        template = "From: bob@example.com\nTo: alice@example.com\nSubject: Re: Test\n\n"
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run = AsyncMock(side_effect=[template, "Message sent"])
            result = await reply(
                email_id="42",
                body="Thanks for the email!",
                folder="INBOX",
            )
            # First call: get template
            first_call = mock_himalaya.run.call_args_list[0]
            assert "template" in first_call[0]
            assert "reply" in first_call[0]
            assert "42" in first_call[0]
            # Second call: send
            second_call = mock_himalaya.run.call_args_list[1]
            assert "template" in second_call[0]
            assert "send" in second_call[0]
            stdin = second_call[1]["stdin"]
            assert "Thanks for the email!" in stdin
            assert result["status"] == "sent"

    async def test_reply_all(self) -> None:
        template = "From: bob@example.com\nTo: alice@example.com\nSubject: Re: Test\n\n"
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run = AsyncMock(side_effect=[template, "Message sent"])
            await reply(email_id="42", body="Thanks!", folder="INBOX", reply_all=True)
            first_call = mock_himalaya.run.call_args_list[0]
            assert "--all" in first_call[0]

    async def test_reply_with_account(self) -> None:
        template = "From: bob@example.com\nTo: alice@example.com\nSubject: Re: Test\n\n"
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run = AsyncMock(side_effect=[template, "Message sent"])
            await reply(email_id="42", body="Thanks!", folder="INBOX", account="work")
            for call in mock_himalaya.run.call_args_list:
                assert call[1]["account"] == "work"


class TestForward:
    async def test_gets_template_edits_and_sends(self) -> None:
        template = "From: bob@example.com\nTo: \nSubject: Fwd: Test\n\n---------- Forwarded message ----------\nOriginal content"
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run = AsyncMock(side_effect=[template, "Message sent"])
            result = await forward(
                email_id="42",
                to="charlie@example.com",
                body="FYI see below",
                folder="INBOX",
            )
            second_call = mock_himalaya.run.call_args_list[1]
            stdin = second_call[1]["stdin"]
            assert "charlie@example.com" in stdin
            assert "FYI see below" in stdin
            assert result["status"] == "sent"

    async def test_forward_with_account(self) -> None:
        template = "From: bob@example.com\nTo: \nSubject: Fwd: Test\n\n"
        with patch("protonmail_mcp.tools.composing.himalaya") as mock_himalaya:
            mock_himalaya.run = AsyncMock(side_effect=[template, "Message sent"])
            await forward(
                email_id="42",
                to="charlie@example.com",
                body="FYI",
                folder="INBOX",
                account="travel",
            )
            for call in mock_himalaya.run.call_args_list:
                assert call[1]["account"] == "travel"
