"""HTML to markdown conversion for email bodies."""

import html2text

_converter = html2text.HTML2Text()
_converter.body_width = 0
_converter.ignore_images = False
_converter.ignore_links = False


def html_to_markdown(html: str | None) -> str:
    """Convert HTML content to readable markdown."""
    if not html:
        return ""
    return _converter.handle(html).strip()
