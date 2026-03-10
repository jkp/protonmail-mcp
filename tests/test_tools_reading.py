"""Tests for reading tools (read_email, download_attachment)."""

from unittest.mock import AsyncMock, patch

from protonmail_mcp.tools.reading import download_attachment, read_email


SAMPLE_TEMPLATE_HTML = (
    "From: Alice <alice@example.com>\n"
    "To: bob@example.com\n"
    "Subject: Test Subject\n"
    "Date: 2026-03-10T08:00:00Z\n"
    "\n"
    "<#part type=text/html>\n"
    "<html><body><p>Hello, this is a <b>test</b> email.</p></body></html>\n"
    "<#/part>\n"
)

SAMPLE_TEMPLATE_PLAIN = (
    "From: Alice <alice@example.com>\n"
    "To: bob@example.com\n"
    "Subject: Plain\n"
    "Date: 2026-03-10\n"
    "\n"
    "<#part type=text/plain>\n"
    "Plain text body\n"
    "<#/part>\n"
)


class TestReadEmail:
    async def test_returns_message_with_markdown_body(self) -> None:
        with patch("protonmail_mcp.tools.reading.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=SAMPLE_TEMPLATE_HTML)
            result = await read_email(email_id="42", folder="INBOX")
            assert result["id"] == "42"
            assert result["subject"] == "Test Subject"
            assert result["from"] == "Alice <alice@example.com>"
            # HTML should be converted to markdown
            assert "**test**" in result["body"]

    async def test_falls_back_to_plain_text(self) -> None:
        with patch("protonmail_mcp.tools.reading.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=SAMPLE_TEMPLATE_PLAIN)
            result = await read_email(email_id="42", folder="INBOX")
            assert result["body"] == "Plain text body"

    async def test_passes_correct_args(self) -> None:
        tpl = "From: x@x.com\nSubject: T\n\nBody"
        with patch("protonmail_mcp.tools.reading.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=tpl)
            await read_email(email_id="42", folder="Sent")
            args = mock_himalaya.run_json.call_args[0]
            assert "message" in args
            assert "read" in args
            assert "42" in args
            assert "--folder" in args
            assert "Sent" in args

    async def test_uses_email_id_as_id(self) -> None:
        tpl = "From: x@x.com\nSubject: T\n\nBody"
        with patch("protonmail_mcp.tools.reading.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=tpl)
            result = await read_email(email_id="99", folder="INBOX")
            assert result["id"] == "99"

    async def test_extracts_cc(self) -> None:
        tpl = "From: a@b.com\nTo: b@c.com\nCc: d@e.com\nSubject: Test\n\nBody"
        with patch("protonmail_mcp.tools.reading.himalaya") as mock_himalaya:
            mock_himalaya.run_json = AsyncMock(return_value=tpl)
            result = await read_email(email_id="42", folder="INBOX")
            assert result["cc"] == "d@e.com"


class TestDownloadAttachment:
    async def test_downloads_to_tempdir(self) -> None:
        with (
            patch("protonmail_mcp.tools.reading.himalaya") as mock_himalaya,
            patch("protonmail_mcp.tools.reading.tempfile") as mock_tempfile,
            patch("protonmail_mcp.tools.reading.Path") as mock_path_cls,
        ):
            mock_himalaya.run = AsyncMock(return_value="")
            mock_tempfile.mkdtemp.return_value = "/tmp/attachments"
            # Mock the file found in temp dir
            mock_file = mock_path_cls.return_value / "doc.pdf"
            mock_path_cls.return_value.glob.return_value = [mock_file]
            mock_file.name = "doc.pdf"
            mock_file.stat.return_value.st_size = 1024
            mock_file.read_bytes.return_value = b"pdf content"

            result = await download_attachment(email_id="42", folder="INBOX", filename="doc.pdf")
            assert result["filename"] == "doc.pdf"
            assert result["size"] == 1024
