"""Tests for reading tools (read_email, list_attachments) and attachment resource."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from protonmail_mcp.tools.reading import attachment_resource, list_attachments, read_email

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


class TestListAttachments:
    async def test_lists_attachments_with_metadata(self, tmp_path) -> None:
        # Create fake downloaded files
        (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 fake")
        (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")

        with patch("protonmail_mcp.tools.reading.himalaya") as mock_himalaya, patch("protonmail_mcp.tools.reading.tempfile") as mock_tempfile:
            mock_himalaya.run = AsyncMock(return_value="")
            mock_tempfile.mkdtemp.return_value = str(tmp_path)

            result = await list_attachments(email_id="42", folder="INBOX")
            assert len(result) == 2
            names = {a["filename"] for a in result}
            assert names == {"report.pdf", "photo.jpg"}
            for a in result:
                assert "size" in a
                assert "mime_type" in a
            pdf = next(a for a in result if a["filename"] == "report.pdf")
            assert pdf["mime_type"] == "application/pdf"

    async def test_empty_when_no_attachments(self, tmp_path) -> None:
        with patch("protonmail_mcp.tools.reading.himalaya") as mock_himalaya, patch("protonmail_mcp.tools.reading.tempfile") as mock_tempfile:
            mock_himalaya.run = AsyncMock(return_value="")
            mock_tempfile.mkdtemp.return_value = str(tmp_path)

            result = await list_attachments(email_id="42", folder="INBOX")
            assert result == []


class TestAttachmentResource:
    async def test_returns_binary_with_mime_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_content = b"%PDF-1.4 test content"
            Path(tmpdir, "report.pdf").write_bytes(pdf_content)

            with patch("protonmail_mcp.tools.reading.himalaya") as mock_himalaya, patch("protonmail_mcp.tools.reading.tempfile") as mock_tempfile:
                mock_himalaya.run = AsyncMock(return_value="")
                mock_tempfile.mkdtemp.return_value = tmpdir

                result = await attachment_resource(folder="INBOX", email_id="42", filename="report.pdf")
                assert len(result.contents) == 1
                assert result.contents[0].content == pdf_content
                assert result.contents[0].mime_type == "application/pdf"

    async def test_raises_on_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("protonmail_mcp.tools.reading.himalaya") as mock_himalaya, patch("protonmail_mcp.tools.reading.tempfile") as mock_tempfile:
                mock_himalaya.run = AsyncMock(return_value="")
                mock_tempfile.mkdtemp.return_value = tmpdir

                try:
                    await attachment_resource(folder="INBOX", email_id="42", filename="nope.pdf")
                    assert False, "Should have raised"
                except FileNotFoundError:
                    pass
