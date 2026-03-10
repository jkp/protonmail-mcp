"""Tests for HTML to markdown conversion."""


from protonmail_mcp.convert import html_to_markdown


class TestHtmlToMarkdown:
    def test_simple_paragraph(self) -> None:
        html = "<p>Hello world</p>"
        result = html_to_markdown(html)
        assert "Hello world" in result

    def test_bold_text(self) -> None:
        html = "<p>This is <b>bold</b> text</p>"
        result = html_to_markdown(html)
        assert "**bold**" in result

    def test_links(self) -> None:
        html = '<p>Click <a href="https://example.com">here</a></p>'
        result = html_to_markdown(html)
        assert "https://example.com" in result
        assert "here" in result

    def test_unordered_list(self) -> None:
        html = "<ul><li>One</li><li>Two</li></ul>"
        result = html_to_markdown(html)
        assert "One" in result
        assert "Two" in result

    def test_none_input(self) -> None:
        assert html_to_markdown(None) == ""

    def test_empty_string(self) -> None:
        assert html_to_markdown("") == ""

    def test_plain_text_passthrough(self) -> None:
        text = "Just plain text"
        result = html_to_markdown(text)
        assert "Just plain text" in result

    def test_complex_email_html(self) -> None:
        html = """
        <html>
        <body>
            <div>
                <p>Hi Bob,</p>
                <p>Please find the <b>report</b> attached.</p>
                <ul>
                    <li>Item 1</li>
                    <li>Item 2</li>
                </ul>
                <p>Best,<br/>Alice</p>
            </div>
        </body>
        </html>
        """
        result = html_to_markdown(html)
        assert "Hi Bob" in result
        assert "**report**" in result
        assert "Item 1" in result
        assert "Alice" in result
