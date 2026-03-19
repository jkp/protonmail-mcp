"""Tests for email_mcp.convert."""

from email_mcp.convert import html_to_markdown


def test_html_to_markdown_basic():
    html = "<h1>Hello</h1><p>World</p>"
    result = html_to_markdown(html)
    assert "Hello" in result
    assert "World" in result


def test_html_to_markdown_empty():
    assert html_to_markdown("") == ""
    assert html_to_markdown(None) == ""


def test_html_to_markdown_links():
    html = '<a href="https://example.com">Click here</a>'
    result = html_to_markdown(html)
    assert "Click here" in result
    # to_text() extracts link text (URLs stripped — fine for LLM consumption)
    assert "<a" not in result


def test_html_to_markdown_strips_whitespace():
    html = "  <p>Hello</p>  "
    result = html_to_markdown(html)
    assert result == result.strip()
