"""Tests for the SQLite database layer (v4 architecture)."""

import json
import time
from pathlib import Path

import pytest

from email_mcp.db import Database, MessageRow, SyncState


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


class TestSchema:
    def test_tables_created(self, db: Database) -> None:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"messages", "message_bodies", "labels", "sync_state"} <= tables

    def test_fts_table_created(self, db: Database) -> None:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='fts_bodies'"
        ).fetchall()
        assert rows

    def test_migrations_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "idem.db"
        Database(path)
        Database(path)  # second open should not raise

    def test_wal_mode(self, db: Database) -> None:
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"


class TestSyncState:
    def test_get_missing_returns_none(self, db: Database) -> None:
        assert db.sync_state.get("last_event_id") is None

    def test_set_and_get(self, db: Database) -> None:
        db.sync_state.set("last_event_id", "abc123")
        assert db.sync_state.get("last_event_id") == "abc123"

    def test_overwrite(self, db: Database) -> None:
        db.sync_state.set("last_event_id", "old")
        db.sync_state.set("last_event_id", "new")
        assert db.sync_state.get("last_event_id") == "new"

    def test_get_with_default(self, db: Database) -> None:
        assert db.sync_state.get("missing", default="fallback") == "fallback"


class TestMessages:
    def _make_row(self, pm_id: str = "pm-001", **kwargs: object) -> MessageRow:
        now = int(time.time())
        return MessageRow(
            pm_id=pm_id,
            message_id=kwargs.get("message_id", f"<{pm_id}@example.com>"),
            subject=kwargs.get("subject", "Test Subject"),
            sender_name=kwargs.get("sender_name", "Alice"),
            sender_email=kwargs.get("sender_email", "alice@example.com"),
            recipients=kwargs.get("recipients", [{"name": "Bob", "email": "bob@example.com"}]),
            date=kwargs.get("date", now),
            unread=kwargs.get("unread", True),
            label_ids=kwargs.get("label_ids", ["0"]),
            folder=kwargs.get("folder", "INBOX"),
            size=kwargs.get("size", 1024),
            has_attachments=kwargs.get("has_attachments", False),
            body_indexed=kwargs.get("body_indexed", False),
        )

    def test_insert_and_get(self, db: Database) -> None:
        row = self._make_row()
        db.messages.upsert(row)
        result = db.messages.get("pm-001")
        assert result is not None
        assert result.pm_id == "pm-001"
        assert result.subject == "Test Subject"
        assert result.folder == "INBOX"

    def test_upsert_updates_existing(self, db: Database) -> None:
        row = self._make_row()
        db.messages.upsert(row)
        updated = self._make_row(subject="Updated Subject", folder="Archive")
        db.messages.upsert(updated)
        result = db.messages.get("pm-001")
        assert result.subject == "Updated Subject"
        assert result.folder == "Archive"

    def test_delete(self, db: Database) -> None:
        db.messages.upsert(self._make_row())
        db.messages.delete("pm-001")
        assert db.messages.get("pm-001") is None

    def test_delete_nonexistent_is_noop(self, db: Database) -> None:
        db.messages.delete("does-not-exist")  # should not raise

    def test_recipients_roundtrip(self, db: Database) -> None:
        recipients = [{"name": "Bob", "email": "bob@example.com"}, {"name": "Carol", "email": "carol@example.com"}]
        db.messages.upsert(self._make_row(recipients=recipients))
        result = db.messages.get("pm-001")
        assert result.recipients == recipients

    def test_label_ids_roundtrip(self, db: Database) -> None:
        db.messages.upsert(self._make_row(label_ids=["0", "5", "custom-label"]))
        result = db.messages.get("pm-001")
        assert result.label_ids == ["0", "5", "custom-label"]

    def test_list_by_folder(self, db: Database) -> None:
        db.messages.upsert(self._make_row("pm-001", folder="INBOX"))
        db.messages.upsert(self._make_row("pm-002", folder="INBOX"))
        db.messages.upsert(self._make_row("pm-003", folder="Archive"))
        results = db.messages.list_by_folder("INBOX", limit=10)
        assert len(results) == 2
        assert all(r.folder == "INBOX" for r in results)

    def test_list_by_folder_ordered_by_date_desc(self, db: Database) -> None:
        now = int(time.time())
        db.messages.upsert(self._make_row("pm-001", folder="INBOX", date=now - 100))
        db.messages.upsert(self._make_row("pm-002", folder="INBOX", date=now))
        results = db.messages.list_by_folder("INBOX", limit=10)
        assert results[0].pm_id == "pm-002"

    def test_list_by_folder_limit(self, db: Database) -> None:
        for i in range(5):
            db.messages.upsert(self._make_row(f"pm-{i:03}", folder="INBOX"))
        results = db.messages.list_by_folder("INBOX", limit=3)
        assert len(results) == 3

    def test_update_folder(self, db: Database) -> None:
        db.messages.upsert(self._make_row(folder="INBOX"))
        db.messages.update_folder("pm-001", "Archive", ["6"])
        result = db.messages.get("pm-001")
        assert result.folder == "Archive"
        assert result.label_ids == ["6"]

    def test_mark_body_indexed(self, db: Database) -> None:
        db.messages.upsert(self._make_row(body_indexed=False))
        db.messages.mark_body_indexed("pm-001")
        result = db.messages.get("pm-001")
        assert result.body_indexed is True

    def test_unindexed_pm_ids(self, db: Database) -> None:
        db.messages.upsert(self._make_row("pm-001", body_indexed=False))
        db.messages.upsert(self._make_row("pm-002", body_indexed=True))
        db.messages.upsert(self._make_row("pm-003", body_indexed=False))
        ids = db.messages.unindexed_pm_ids(limit=10)
        assert set(ids) == {"pm-001", "pm-003"}


class TestBodies:
    def test_insert_and_fts_search(self, db: Database) -> None:
        now = int(time.time())
        db.messages.upsert(MessageRow(
            pm_id="pm-001", message_id="<1@ex.com>",
            subject="Invoice", sender_name="Bob", sender_email="bob@ex.com",
            recipients=[], date=now, unread=True, label_ids=["0"],
            folder="INBOX", size=100, has_attachments=False, body_indexed=False,
        ))
        db.bodies.insert("pm-001", "Please find the invoice attached for services rendered")
        results = db.bodies.search("invoice services", limit=10)
        assert "pm-001" in results

    def test_fts_no_match(self, db: Database) -> None:
        results = db.bodies.search("xyzzy", limit=10)
        assert results == []

    def test_delete_cascades(self, db: Database) -> None:
        now = int(time.time())
        db.messages.upsert(MessageRow(
            pm_id="pm-001", message_id="<1@ex.com>",
            subject="Test", sender_name="A", sender_email="a@ex.com",
            recipients=[], date=now, unread=False, label_ids=[],
            folder="INBOX", size=0, has_attachments=False, body_indexed=True,
        ))
        db.bodies.insert("pm-001", "some body text")
        db.messages.delete("pm-001")
        results = db.bodies.search("body", limit=10)
        assert "pm-001" not in results


class TestLabels:
    def test_upsert_and_list(self, db: Database) -> None:
        db.labels.upsert("0", "INBOX", type=3)
        db.labels.upsert("6", "Archive", type=3)
        db.labels.upsert("custom-1", "Work", type=2)
        labels = db.labels.all()
        names = {lb["name"] for lb in labels}
        assert {"INBOX", "Archive", "Work"} <= names

    def test_name_for_id(self, db: Database) -> None:
        db.labels.upsert("6", "Archive", type=3)
        assert db.labels.name_for_id("6") == "Archive"
        assert db.labels.name_for_id("missing") is None
