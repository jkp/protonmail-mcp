"""HTML to markdown conversion for email bodies."""

import re

from justhtml import JustHTML

# Matches common HTML start patterns (with optional whitespace/BOM)
_HTML_PATTERN = re.compile(
    r"^\s*(<\!DOCTYPE|<html|<head|<body|<div|<p\b|<table|<span)",
    re.IGNORECASE,
)


def _is_html(text: str) -> bool:
    """Detect if text is HTML content."""
    return bool(_HTML_PATTERN.match(text))


def html_to_markdown(html: str | None) -> str:
    """Convert HTML content to readable markdown.

    Uses justhtml — a spec-compliant HTML5 parser with browser-grade
    error recovery. Handles malformed Office HTML that crashes html2text.
    """
    if not html:
        return ""
    doc = JustHTML(html, sanitize=False)
    return doc.to_markdown().strip()


def body_for_display(body: str) -> str:
    """Convert an email body to LLM-friendly format.

    HTML bodies are converted to markdown (70-80% token reduction).
    Plaintext bodies are returned as-is.

    Use this everywhere email bodies are returned to the LLM:
    read_email, batch_read, composing, etc.
    """
    if not body:
        return ""
    if _is_html(body):
        return html_to_markdown(body)
    return body
