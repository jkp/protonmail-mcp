"""Tests for himalaya template parser."""

from protonmail_mcp.template import parse_template


class TestParseTemplate:
    def test_extracts_headers(self) -> None:
        tpl = "From: Alice <alice@example.com>\nTo: bob@example.com\nSubject: Hello\n\nBody text"
        result = parse_template(tpl)
        assert result["from"] == "Alice <alice@example.com>"
        assert result["to"] == "bob@example.com"
        assert result["subject"] == "Hello"

    def test_extracts_html_part(self) -> None:
        tpl = "From: a@b.com\nSubject: Test\n\n<#part type=text/html>\n<p>Hello <b>world</b></p>\n<#/part>\n"
        result = parse_template(tpl)
        assert result["text/html"] == "<p>Hello <b>world</b></p>"

    def test_extracts_plain_text_part(self) -> None:
        tpl = "From: a@b.com\nSubject: Test\n\n<#part type=text/plain>\nHello world\n<#/part>\n"
        result = parse_template(tpl)
        assert result["text/plain"] == "Hello world"

    def test_body_without_part_markers(self) -> None:
        tpl = "From: a@b.com\nSubject: Test\n\nPlain body text here"
        result = parse_template(tpl)
        assert result["text/plain"] == "Plain body text here"

    def test_multiple_parts(self) -> None:
        tpl = (
            "From: a@b.com\nSubject: Test\n\n"
            "<#part type=text/plain>\nPlain version\n<#/part>\n"
            "<#part type=text/html>\n<p>HTML version</p>\n<#/part>\n"
        )
        result = parse_template(tpl)
        assert result["text/plain"] == "Plain version"
        assert result["text/html"] == "<p>HTML version</p>"

    def test_cc_header(self) -> None:
        tpl = "From: a@b.com\nTo: b@c.com\nCc: d@e.com\nSubject: Test\n\nBody"
        result = parse_template(tpl)
        assert result["cc"] == "d@e.com"

    def test_missing_headers_default_empty(self) -> None:
        tpl = "Subject: Minimal\n\nBody"
        result = parse_template(tpl)
        assert result["from"] == ""
        assert result["to"] == ""
        assert result["cc"] == ""

    def test_date_header(self) -> None:
        tpl = "From: a@b.com\nDate: Mon, 10 Mar 2026 08:00:00 +0000\nSubject: Test\n\nBody"
        result = parse_template(tpl)
        assert result["date"] == "Mon, 10 Mar 2026 08:00:00 +0000"

    def test_real_himalaya_output(self) -> None:
        """Test with realistic himalaya template output."""
        tpl = (
            "From: Hargreaves Lansdown <hl@service.hl.co.uk>\n"
            "To: jamie@kirkpatrick.email\n"
            "Subject: Tax Year End\n"
            "\n"
            "<#part type=text/html>\n"
            "<html><body><p>Important tax info</p></body></html>\n"
            "<#/part>\n"
        )
        result = parse_template(tpl)
        assert result["from"] == "Hargreaves Lansdown <hl@service.hl.co.uk>"
        assert result["to"] == "jamie@kirkpatrick.email"
        assert result["subject"] == "Tax Year End"
        assert "<p>Important tax info</p>" in result["text/html"]
