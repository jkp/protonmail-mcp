"""Tests for reading tools (read_email, list_attachments, download_attachment)."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastmcp.utilities.types import Image

from protonmail_mcp.tools.reading import download_attachment, list_attachments, read_email

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


class TestDownloadAttachment:
    async def test_pdf_returns_extracted_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a minimal valid PDF
            import pymupdf

            doc = pymupdf.open()
            page = doc.new_page()
            page.insert_text((72, 72), "Hello from PDF")
            doc.save(str(Path(tmpdir) / "report.pdf"))
            doc.close()

            with patch("protonmail_mcp.tools.reading.himalaya") as mock_himalaya, patch("protonmail_mcp.tools.reading.tempfile") as mock_tempfile:
                mock_himalaya.run = AsyncMock(return_value="")
                mock_tempfile.mkdtemp.return_value = tmpdir

                result = await download_attachment(email_id="42", folder="INBOX", filename="report.pdf")
                assert len(result) == 1
                assert isinstance(result[0], str)
                assert "report.pdf" in result[0]
                assert "Hello from PDF" in result[0]

    async def test_image_returns_image_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a minimal PNG (1x1 pixel)
            import struct
            import zlib

            def _minimal_png() -> bytes:
                sig = b"\x89PNG\r\n\x1a\n"
                ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
                ihdr = _chunk(b"IHDR", ihdr_data)
                raw = b"\x00\xff\x00\x00"  # filter byte + RGB
                idat = _chunk(b"IDAT", zlib.compress(raw))
                iend = _chunk(b"IEND", b"")
                return sig + ihdr + idat + iend

            def _chunk(chunk_type: bytes, data: bytes) -> bytes:
                c = chunk_type + data
                return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

            (Path(tmpdir) / "photo.png").write_bytes(_minimal_png())

            with patch("protonmail_mcp.tools.reading.himalaya") as mock_himalaya, patch("protonmail_mcp.tools.reading.tempfile") as mock_tempfile:
                mock_himalaya.run = AsyncMock(return_value="")
                mock_tempfile.mkdtemp.return_value = tmpdir

                result = await download_attachment(email_id="42", folder="INBOX", filename="photo.png")
                assert len(result) == 2
                assert isinstance(result[0], str)
                assert "photo.png" in result[0]
                assert isinstance(result[1], Image)

    async def test_text_file_returns_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "data.csv").write_text("name,age\nAlice,30\nBob,25")

            with patch("protonmail_mcp.tools.reading.himalaya") as mock_himalaya, patch("protonmail_mcp.tools.reading.tempfile") as mock_tempfile:
                mock_himalaya.run = AsyncMock(return_value="")
                mock_tempfile.mkdtemp.return_value = tmpdir

                result = await download_attachment(email_id="42", folder="INBOX", filename="data.csv")
                assert len(result) == 1
                assert "name,age" in result[0]
                assert "Alice,30" in result[0]

    async def test_unknown_binary_returns_base64(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "data.bin").write_bytes(b"\x00\x01\x02\x03")

            with patch("protonmail_mcp.tools.reading.himalaya") as mock_himalaya, patch("protonmail_mcp.tools.reading.tempfile") as mock_tempfile:
                mock_himalaya.run = AsyncMock(return_value="")
                mock_tempfile.mkdtemp.return_value = tmpdir

                result = await download_attachment(email_id="42", folder="INBOX", filename="data.bin")
                assert len(result) == 1
                assert "Base64" in result[0]
                assert "AAECAw==" in result[0]

    async def test_missing_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("protonmail_mcp.tools.reading.himalaya") as mock_himalaya, patch("protonmail_mcp.tools.reading.tempfile") as mock_tempfile:
                mock_himalaya.run = AsyncMock(return_value="")
                mock_tempfile.mkdtemp.return_value = tmpdir

                try:
                    await download_attachment(email_id="42", folder="INBOX", filename="nope.pdf")
                    assert False, "Should have raised"
                except FileNotFoundError:
                    pass
