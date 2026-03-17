"""SQLite database layer for v4 architecture.

Single-file SQLite database with:
  - messages      — metadata for all messages (keyed by ProtonMail UUID)
  - message_bodies — decrypted plaintext bodies
  - fts_bodies    — FTS5 full-text index over bodies
  - labels        — ProtonMail label/folder definitions
  - sync_state    — event loop cursor and sync flags
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ── Schema ──────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS messages (
    pm_id           TEXT PRIMARY KEY,
    message_id      TEXT UNIQUE,
    subject         TEXT,
    sender_name     TEXT,
    sender_email    TEXT,
    recipients      TEXT    NOT NULL DEFAULT '[]',
    date            INTEGER NOT NULL,
    unread          INTEGER NOT NULL DEFAULT 1,
    label_ids       TEXT    NOT NULL DEFAULT '[]',
    folder          TEXT,
    size            INTEGER,
    has_attachments INTEGER NOT NULL DEFAULT 0,
    body_indexed    INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at      INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_messages_folder ON messages(folder);
CREATE INDEX IF NOT EXISTS idx_messages_date   ON messages(date DESC);
CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages(unread) WHERE unread = 1;
CREATE INDEX IF NOT EXISTS idx_messages_body   ON messages(body_indexed) WHERE body_indexed = 0;

CREATE TABLE IF NOT EXISTS message_bodies (
    rowid  INTEGER PRIMARY KEY,
    pm_id  TEXT NOT NULL UNIQUE REFERENCES messages(pm_id) ON DELETE CASCADE,
    body   TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_bodies USING fts5(
    pm_id UNINDEXED,
    body,
    content='message_bodies',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS bodies_ai AFTER INSERT ON message_bodies BEGIN
    INSERT INTO fts_bodies(rowid, pm_id, body) VALUES (new.rowid, new.pm_id, new.body);
END;
CREATE TRIGGER IF NOT EXISTS bodies_ad AFTER DELETE ON message_bodies BEGIN
    INSERT INTO fts_bodies(fts_bodies, rowid, pm_id, body)
    VALUES ('delete', old.rowid, old.pm_id, old.body);
END;
CREATE TRIGGER IF NOT EXISTS bodies_au AFTER UPDATE ON message_bodies BEGIN
    INSERT INTO fts_bodies(fts_bodies, rowid, pm_id, body)
    VALUES ('delete', old.rowid, old.pm_id, old.body);
    INSERT INTO fts_bodies(rowid, pm_id, body) VALUES (new.rowid, new.pm_id, new.body);
END;

CREATE TABLE IF NOT EXISTS labels (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    type    INTEGER NOT NULL,
    color   TEXT,
    display_order INTEGER
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class MessageRow:
    pm_id: str
    message_id: str | None
    subject: str | None
    sender_name: str | None
    sender_email: str | None
    recipients: list[dict[str, str]]
    date: int
    unread: bool
    label_ids: list[str]
    folder: str | None
    size: int | None
    has_attachments: bool
    body_indexed: bool
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))


# ── Accessors ────────────────────────────────────────────────────────────────

class _SyncStateAccessor:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM sync_state WHERE key = ?", [key]
        ).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            [key, value],
        )
        self._conn.commit()


class _MessagesAccessor:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, row: MessageRow) -> None:
        now = int(time.time())
        self._conn.execute(
            """
            INSERT INTO messages (
                pm_id, message_id, subject, sender_name, sender_email,
                recipients, date, unread, label_ids, folder, size,
                has_attachments, body_indexed, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pm_id) DO UPDATE SET
                message_id      = excluded.message_id,
                subject         = excluded.subject,
                sender_name     = excluded.sender_name,
                sender_email    = excluded.sender_email,
                recipients      = excluded.recipients,
                date            = excluded.date,
                unread          = excluded.unread,
                label_ids       = excluded.label_ids,
                folder          = excluded.folder,
                size            = excluded.size,
                has_attachments = excluded.has_attachments,
                body_indexed    = excluded.body_indexed,
                updated_at      = excluded.updated_at
            """,
            [
                row.pm_id, row.message_id, row.subject,
                row.sender_name, row.sender_email,
                json.dumps(row.recipients), row.date, int(row.unread),
                json.dumps(row.label_ids), row.folder, row.size,
                int(row.has_attachments), int(row.body_indexed),
                row.created_at, now,
            ],
        )
        self._conn.commit()

    def get(self, pm_id: str) -> MessageRow | None:
        row = self._conn.execute(
            "SELECT * FROM messages WHERE pm_id = ?", [pm_id]
        ).fetchone()
        return _row_to_message(row) if row else None

    def delete(self, pm_id: str) -> None:
        self._conn.execute("DELETE FROM messages WHERE pm_id = ?", [pm_id])
        self._conn.commit()

    def list_by_folder(self, folder: str, limit: int, offset: int = 0) -> list[MessageRow]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE folder = ? ORDER BY date DESC LIMIT ? OFFSET ?",
            [folder, limit, offset],
        ).fetchall()
        return [_row_to_message(r) for r in rows]

    def update_folder(self, pm_id: str, folder: str, label_ids: list[str]) -> None:
        self._conn.execute(
            "UPDATE messages SET folder = ?, label_ids = ?, updated_at = ? WHERE pm_id = ?",
            [folder, json.dumps(label_ids), int(time.time()), pm_id],
        )
        self._conn.commit()

    def mark_body_indexed(self, pm_id: str) -> None:
        self._conn.execute(
            "UPDATE messages SET body_indexed = 1, updated_at = ? WHERE pm_id = ?",
            [int(time.time()), pm_id],
        )
        self._conn.commit()

    def unindexed_pm_ids(self, limit: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT pm_id FROM messages WHERE body_indexed = 0 ORDER BY date DESC LIMIT ?",
            [limit],
        ).fetchall()
        return [r[0] for r in rows]

    def count_by_folder(self, folder: str) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN unread = 1 THEN 1 ELSE 0 END) FROM messages WHERE folder = ?",
            [folder],
        ).fetchone()
        return {"total": rows[0] or 0, "unread": rows[1] or 0}


class _BodiesAccessor:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, pm_id: str, body: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO message_bodies (pm_id, body) VALUES (?, ?)",
            [pm_id, body],
        )
        self._conn.commit()

    def get(self, pm_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT body FROM message_bodies WHERE pm_id = ?", [pm_id]
        ).fetchone()
        return row[0] if row else None

    def search(self, query: str, limit: int) -> list[str]:
        """Full-text search. Returns list of pm_ids ordered by relevance."""
        try:
            rows = self._conn.execute(
                "SELECT pm_id FROM fts_bodies WHERE fts_bodies MATCH ? ORDER BY rank LIMIT ?",
                [query, limit],
            ).fetchall()
            return [r[0] for r in rows]
        except sqlite3.OperationalError:
            return []


class _LabelsAccessor:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, id: str, name: str, type: int, color: str | None = None, order: int | None = None) -> None:
        self._conn.execute(
            """
            INSERT INTO labels (id, name, type, color, display_order)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name, type = excluded.type,
                color = excluded.color, display_order = excluded.display_order
            """,
            [id, name, type, color, order],
        )
        self._conn.commit()

    def all(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT id, name, type, color, display_order FROM labels").fetchall()
        return [{"id": r[0], "name": r[1], "type": r[2], "color": r[3], "order": r[4]} for r in rows]

    def name_for_id(self, label_id: str) -> str | None:
        row = self._conn.execute("SELECT name FROM labels WHERE id = ?", [label_id]).fetchone()
        return row[0] if row else None


# ── Database ──────────────────────────────────────────────────────────────────

class Database:
    """SQLite database for v4 email-mcp.

    Usage:
        db = Database(Path("~/.local/share/email-mcp/db.sqlite"))
        db.messages.upsert(row)
        db.bodies.insert(pm_id, body_text)
        db.bodies.search("invoice services", limit=20)
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._apply_schema()

        self.sync_state = _SyncStateAccessor(self._conn)
        self.messages = _MessagesAccessor(self._conn)
        self.bodies = _BodiesAccessor(self._conn)
        self.labels = _LabelsAccessor(self._conn)

    def execute(self, sql: str, params: list[Any] | None = None) -> sqlite3.Cursor:
        return self._conn.execute(sql, params or [])

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _apply_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_message(row: sqlite3.Row) -> MessageRow:
    return MessageRow(
        pm_id=row["pm_id"],
        message_id=row["message_id"],
        subject=row["subject"],
        sender_name=row["sender_name"],
        sender_email=row["sender_email"],
        recipients=json.loads(row["recipients"]),
        date=row["date"],
        unread=bool(row["unread"]),
        label_ids=json.loads(row["label_ids"]),
        folder=row["folder"],
        size=row["size"],
        has_attachments=bool(row["has_attachments"]),
        body_indexed=bool(row["body_indexed"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Unused import for re-export (tests import SyncState directly) ─────────────
SyncState = _SyncStateAccessor
