"""Tests for the embedding pipeline and vector search."""

import time
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from email_mcp.db import Database, MessageRow
from email_mcp.embedder import Embedder


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


def _insert_message(
    db: Database,
    pm_id: str,
    subject: str = "Test",
    sender_name: str = "Alice",
    sender_email: str = "alice@example.com",
    body: str | None = None,
) -> None:
    db.messages.upsert(
        MessageRow(
            pm_id=pm_id,
            message_id=f"{pm_id}@example.com",
            subject=subject,
            sender_name=sender_name,
            sender_email=sender_email,
            recipients=[],
            date=int(time.time()),
            unread=False,
            label_ids=["0"],
            folder="INBOX",
            size=1024,
            has_attachments=False,
            body_indexed=bool(body),
        )
    )
    if body:
        db.bodies.insert(pm_id, body)
        db.messages.mark_body_indexed(pm_id)


@pytest.fixture
def mock_model():
    """Mock sentence transformer that returns deterministic vectors."""
    model = MagicMock()

    def _encode(texts, batch_size=64):
        # Return distinct vectors based on text content hash
        vecs = []
        for t in texts:
            rng = np.random.RandomState(hash(t) % 2**31)
            vecs.append(rng.randn(384).astype(np.float32))
        return np.array(vecs)

    model.encode = _encode
    return model


@pytest.fixture
def embedder(db: Database, mock_model) -> Embedder:
    return Embedder(db=db, model=mock_model)


class TestEmbedBatch:
    def test_embeds_messages_with_bodies(self, embedder, db):
        _insert_message(db, "pm-1", body="Hello from Alice")
        _insert_message(db, "pm-2", body="Hello from Bob")

        embedder.embed_batch(["pm-1", "pm-2"])

        msg1 = db.messages.get("pm-1")
        msg2 = db.messages.get("pm-2")
        assert msg1.embedded is True
        assert msg2.embedded is True

    def test_skips_messages_without_bodies(self, embedder, db):
        _insert_message(db, "pm-1")  # no body

        embedder.embed_batch(["pm-1"])

        msg = db.messages.get("pm-1")
        assert msg.embedded is False

    def test_skips_unknown_pm_ids(self, embedder, db):
        embedder.embed_batch(["nonexistent"])  # should not raise

    def test_empty_batch(self, embedder, db):
        embedder.embed_batch([])  # should not raise


class TestVectorSearch:
    def test_finds_similar_messages(self, embedder, db):
        _insert_message(
            db,
            "pm-1",
            subject="Headphone cable",
            sender_name="Benson",
            body="The SR-Omega cable is ready for pickup",
        )
        _insert_message(
            db,
            "pm-2",
            subject="Invoice",
            sender_name="Accounting",
            body="Please find attached invoice for services",
        )
        embedder.embed_batch(["pm-1", "pm-2"])

        results = embedder.search("benson headphones", limit=5)
        assert len(results) > 0
        # Results are pm_ids
        assert all(isinstance(r, str) for r in results)

    def test_returns_empty_for_no_vectors(self, embedder, db):
        results = embedder.search("anything", limit=5)
        assert results == []

    def test_respects_limit(self, embedder, db):
        for i in range(10):
            _insert_message(db, f"pm-{i}", body=f"Email number {i}")
        embedder.embed_batch([f"pm-{i}" for i in range(10)])

        results = embedder.search("email", limit=3)
        assert len(results) <= 3


class TestUnembeddedQuery:
    def test_returns_unembedded_pm_ids(self, embedder, db):
        _insert_message(db, "pm-1", body="Hello")
        _insert_message(db, "pm-2", body="World")

        unembedded = embedder.get_unembedded(limit=10)
        assert len(unembedded) == 2

    def test_respects_priority_order(self, embedder, db):
        _insert_message(db, "pm-1", body="Archive msg")
        db.execute(
            "UPDATE messages SET folder = 'Archive' WHERE pm_id = 'pm-1'"
        )
        db.commit()
        _insert_message(db, "pm-2", body="Inbox msg")

        unembedded = embedder.get_unembedded(limit=10)
        # INBOX should come first
        assert unembedded[0] == "pm-2"

    def test_skips_already_embedded(self, embedder, db):
        _insert_message(db, "pm-1", body="Hello")
        embedder.embed_batch(["pm-1"])

        unembedded = embedder.get_unembedded(limit=10)
        assert unembedded == []
