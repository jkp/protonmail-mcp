"""Gmail-style query → SQL translator for SQLite FTS5 search.

Supported operators:
    from:alice          sender_email LIKE '%alice%'
    subject:invoice     subject LIKE '%invoice%'
    is:unread           unread = 1
    is:read             unread = 0
    in:inbox            folder = 'INBOX'
    in:archive          folder = 'Archive'
    in:trash            folder = 'Trash'
    in:sent             folder = 'Sent'
    in:spam             folder = 'Spam'
    has:attachment(s)   has_attachments = 1
    filename:report     subquery on attachments table
    older_than:30d      date < unixepoch() - N  (h/d/w/m/y units)
    newer_than:7d       date > unixepoch() - N
    *                   match all (no constraint)
    free text           FTS5 MATCH

Multiple operators are ANDed together.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

# Folder name normalisation
_FOLDER_MAP = {
    "inbox": "INBOX",
    "archive": "Archive",
    "trash": "Trash",
    "sent": "Sent",
    "spam": "Spam",
    "drafts": "Drafts",
    "all": "All Mail",
}

# Duration suffix → seconds
_DURATION_SECONDS = {
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "m": 2592000,    # 30 days
    "y": 31536000,   # 365 days
}

_OPERATOR_RE = re.compile(
    r'(\w[\w_]*):("(?:[^"]+)"|[^\s]+)'
)
_DURATION_RE = re.compile(r'^(\d+)([hdwmy])$')


@dataclass
class ParsedQuery:
    where_clauses: list[str] = field(default_factory=list)
    params: list[Any] = field(default_factory=list)
    fts_terms: str | None = None

    @property
    def where(self) -> str:
        if not self.where_clauses:
            return "1"
        return " AND ".join(self.where_clauses)

    def to_sql(self, limit: int, offset: int = 0) -> tuple[str, list[Any]]:
        """Generate a complete SELECT SQL and parameter list."""
        if self.fts_terms:
            sql = """
                SELECT m.*
                FROM messages m
                JOIN message_bodies mb ON mb.pm_id = m.pm_id
                JOIN fts_bodies f ON f.rowid = mb.rowid
                WHERE fts_bodies MATCH ?
                {where_filter}
                ORDER BY rank, m.date DESC
                LIMIT ? OFFSET ?
            """.format(
                where_filter=f"AND {self.where}" if self.where != "1" else ""
            )
            params = [self.fts_terms, *self.params, limit, offset]
        else:
            sql = """
                SELECT * FROM messages
                WHERE {where}
                ORDER BY date DESC
                LIMIT ? OFFSET ?
            """.format(where=self.where)
            params = [*self.params, limit, offset]

        return sql, params


def build_query(query: str) -> ParsedQuery:
    """Parse a Gmail-style query string into a ParsedQuery."""
    query = query.strip()
    if not query or query == "*":
        return ParsedQuery()

    result = ParsedQuery()
    remaining = query

    for match in _OPERATOR_RE.finditer(query):
        op = match.group(1).lower()
        val = match.group(2).strip('"')
        remaining = remaining.replace(match.group(0), "", 1).strip()
        _apply_operator(result, op, val)

    # Whatever's left is free-text for FTS
    fts = remaining.strip()
    if fts:
        result.fts_terms = _sanitize_fts(fts)

    return result


def _sanitize_fts(text: str) -> str:
    """Sanitize free-text for FTS5 MATCH.

    FTS5 treats -, +, *, etc. as operators. Wrap tokens containing
    special chars in double quotes so they're treated as literals.
    Already-quoted phrases are left as-is.
    """
    tokens = []
    for part in re.findall(r'"[^"]*"|\S+', text):
        if part.startswith('"') and part.endswith('"'):
            tokens.append(part)  # already quoted
        elif re.search(r'[^\w\s]', part):
            tokens.append(f'"{part}"')  # quote it
        else:
            tokens.append(part)
    return " ".join(tokens)


def _apply_operator(result: ParsedQuery, op: str, val: str) -> None:
    if op == "from":
        result.where_clauses.append("sender_email LIKE ?")
        result.params.append(f"%{val}%")

    elif op == "to":
        result.where_clauses.append("recipients LIKE ?")
        result.params.append(f"%{val}%")

    elif op == "subject":
        result.where_clauses.append("subject LIKE ?")
        result.params.append(f"%{val}%")

    elif op == "is":
        v = val.lower()
        if v == "unread":
            result.where_clauses.append("unread = 1")
        elif v == "read":
            result.where_clauses.append("unread = 0")
        # starred, flagged etc. — ignore unknown values gracefully

    elif op == "in":
        folder = _FOLDER_MAP.get(val.lower())
        if folder:
            result.where_clauses.append("folder = ?")
            result.params.append(folder)

    elif op == "has":
        if val.lower().startswith("attachment"):
            result.where_clauses.append("has_attachments = 1")

    elif op == "filename":
        # Subquery: message must have an attachment with matching filename
        result.where_clauses.append(
            "pm_id IN (SELECT pm_id FROM attachments WHERE filename LIKE ?)"
        )
        result.params.append(f"%{val}%")

    elif op in ("older_than", "newer_than"):
        seconds = _parse_duration(val)
        if seconds is not None:
            cutoff = int(time.time()) - seconds
            if op == "older_than":
                result.where_clauses.append("date < ?")
            else:
                result.where_clauses.append("date > ?")
            result.params.append(cutoff)

    # Unknown operators are silently ignored


def _parse_duration(val: str) -> int | None:
    """Parse a duration string like '30d', '12h', '2w' into seconds."""
    m = _DURATION_RE.match(val.lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return n * _DURATION_SECONDS.get(unit, 86400)
