"""HTML to markdown conversion for email bodies."""

import quopri
import re

from justhtml import JustHTML

# Matches HTML tags anywhere in the body
_HTML_PATTERN = re.compile(
    r"<(!DOCTYPE|html|head|body|div|p\b|table|span|br\s*/?>|img\b)",
    re.IGNORECASE,
)


def _is_html(text: str) -> bool:
    """Detect if text contains HTML content."""
    return bool(_HTML_PATTERN.search(text))


_STRIP_TAGS = re.compile(r"<[^>]+>")


def html_to_markdown(html: str | None) -> str:
    """Convert HTML content to readable markdown.

    Uses justhtml — a spec-compliant HTML5 parser with browser-grade
    error recovery. Handles malformed Office HTML that crashes html2text.

    to_markdown() converts most HTML well (paragraphs, links, headings,
    lists) but leaves tables as raw HTML. We strip leftover tags while
    preserving whitespace so paragraph breaks survive (to_text() collapses
    everything into one line).
    """
    if not html:
        return ""
    doc = JustHTML(html, sanitize=False)
    md = doc.to_markdown()

    # Strip leftover HTML tags (tables, imgs) but keep surrounding whitespace
    if "<" in md:
        md = _STRIP_TAGS.sub("", md)
        md = re.sub(r"\n{3,}", "\n\n", md)

    return md.strip()


_QP_PATTERN = re.compile(r"=[0-9A-Fa-f]{2}")


def _decode_qp(text: str) -> str:
    """Decode quoted-printable if the text contains QP sequences."""
    if not _QP_PATTERN.search(text):
        return text
    try:
        return quopri.decodestring(text.encode("ascii", errors="replace")).decode(
            "utf-8", errors="replace"
        )
    except Exception:
        return text


def body_for_display(body: str) -> str:
    """Convert an email body to LLM-friendly format.

    Decodes quoted-printable, then converts HTML to markdown.
    Plaintext bodies are returned as-is.

    Use this everywhere email bodies are returned to the LLM:
    read_email, batch_read, composing, etc.
    """
    if not body:
        return ""
    body = _decode_qp(body)
    if _is_html(body):
        return html_to_markdown(body)
    return body
