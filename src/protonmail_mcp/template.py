"""Parse himalaya template format into structured data."""

import re


def parse_template(template: str) -> dict[str, str]:
    """Parse a himalaya template string into headers and body parts.

    Himalaya's `message read --output json` returns a JSON-encoded string
    in template format: RFC2822 headers, blank line, then body with
    `<#part type=...>` / `<#/part>` markers for MIME parts.
    """
    # Split headers from body at first blank line
    parts = template.split("\n\n", 1)
    header_block = parts[0]
    body_block = parts[1] if len(parts) > 1 else ""

    # Parse headers
    headers: dict[str, str] = {}
    for line in header_block.splitlines():
        match = re.match(r"^([A-Za-z-]+):\s*(.*)$", line)
        if match:
            headers[match.group(1).lower()] = match.group(2).strip()

    # Parse body parts
    body_parts: dict[str, str] = {}
    part_pattern = re.compile(r"<#part\s+type=([^\s>]+)>\s*\n?(.*?)\n?\s*<#/part>", re.DOTALL)
    for m in part_pattern.finditer(body_block):
        body_parts[m.group(1)] = m.group(2).strip()

    # If no part markers, treat entire body as plain text
    if not body_parts and body_block.strip():
        body_parts["text/plain"] = body_block.strip()

    return {
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "text/plain": body_parts.get("text/plain", ""),
        "text/html": body_parts.get("text/html", ""),
    }
