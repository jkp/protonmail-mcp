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

    def _encode(texts, batch_size=64, show_progress_bar=True):
        # Return distinct unit-normalized vectors based on text content hash.
        # Normalized so cosine distances are in [0, 2] as sqlite-vec expects.
        from email_mcp.embedder import _EMBEDDING_DIMS

        vecs = []
        for t in texts:
            rng = np.random.RandomState(hash(t) % 2**31)
            v = rng.randn(_EMBEDDING_DIMS).astype(np.float32)
            v /= np.linalg.norm(v)  # unit normalize
            vecs.append(v)
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

        # -1 = nothing to embed (empty body), won't be re-queued
        row = db.execute("SELECT embedded FROM messages WHERE pm_id = 'pm-1'").fetchone()
        assert row[0] == -1

    def test_skips_unknown_pm_ids(self, embedder, db):
        embedder.embed_batch(["nonexistent"])  # should not raise

    def test_empty_batch(self, embedder, db):
        embedder.embed_batch([])  # should not raise


class TestVectorSearch:
    def test_finds_similar_messages(self, embedder, db, monkeypatch):
        # The mock model returns hash-based random unit vectors, so two
        # different texts produce ~orthogonal vectors (distance ≈ 1.0).
        # This test checks search mechanics, not semantic similarity.
        # No distance threshold to worry about — reranker handles precision.
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


class TestSkipFilter:
    def test_exact_sender_match_skipped(self, db, mock_model):
        emb = Embedder(
            db=db,
            model=mock_model,
            skip_senders=["notifications@github.com"],
        )
        _insert_message(db, "pm-1", sender_email="notifications@github.com", body="PR opened")
        emb.embed_batch(["pm-1"])
        row = db.execute("SELECT embedded FROM messages WHERE pm_id = 'pm-1'").fetchone()
        assert row[0] == -1

    def test_domain_match_skipped(self, db, mock_model):
        emb = Embedder(db=db, model=mock_model, skip_domains=["amazon.co.uk"])
        _insert_message(db, "pm-1", sender_email="orders@amazon.co.uk", body="Your order")
        _insert_message(db, "pm-2", sender_email="alice@example.com", body="Hello")
        emb.embed_batch(["pm-1", "pm-2"])
        row1 = db.execute("SELECT embedded FROM messages WHERE pm_id = 'pm-1'").fetchone()
        row2 = db.execute("SELECT embedded FROM messages WHERE pm_id = 'pm-2'").fetchone()
        assert row1[0] == -1
        assert row2[0] == 1

    def test_glob_match_skipped(self, db, mock_model):
        emb = Embedder(db=db, model=mock_model, skip_senders=["noreply@*"])
        _insert_message(db, "pm-1", sender_email="noreply@stripe.com", body="Receipt")
        emb.embed_batch(["pm-1"])
        row = db.execute("SELECT embedded FROM messages WHERE pm_id = 'pm-1'").fetchone()
        assert row[0] == -1

    def test_match_is_case_insensitive(self, db, mock_model):
        emb = Embedder(
            db=db,
            model=mock_model,
            skip_senders=["NOTIFICATIONS@github.com"],
            skip_domains=["EBAY.COM"],
        )
        _insert_message(db, "pm-1", sender_email="Notifications@GitHub.com", body="x")
        _insert_message(db, "pm-2", sender_email="seller@Ebay.com", body="y")
        emb.embed_batch(["pm-1", "pm-2"])
        rows = db.execute("SELECT pm_id, embedded FROM messages ORDER BY pm_id").fetchall()
        assert {r[0]: r[1] for r in rows} == {"pm-1": -1, "pm-2": -1}

    def test_non_matching_sender_still_embedded(self, db, mock_model):
        emb = Embedder(
            db=db,
            model=mock_model,
            skip_senders=["notifications@github.com"],
            skip_domains=["amazon.co.uk"],
        )
        _insert_message(db, "pm-1", sender_email="alice@example.com", body="Hello")
        emb.embed_batch(["pm-1"])
        msg = db.messages.get("pm-1")
        assert msg.embedded is True

    def test_skipped_messages_not_returned_by_get_unembedded(self, db, mock_model):
        emb = Embedder(db=db, model=mock_model, skip_senders=["notifications@github.com"])
        _insert_message(db, "pm-1", sender_email="notifications@github.com", body="PR")
        _insert_message(db, "pm-2", sender_email="alice@example.com", body="Hi")
        emb.embed_batch(["pm-1", "pm-2"])
        # pm-1 was filtered (-1), pm-2 was embedded (1) — neither is unembedded (0)
        assert emb.get_unembedded(limit=10) == []


class TestMarkSkippedExisting:
    def test_marks_existing_matching_rows(self, db, mock_model):
        _insert_message(db, "pm-1", sender_email="notifications@github.com", body="x")
        _insert_message(db, "pm-2", sender_email="orders@amazon.co.uk", body="y")
        _insert_message(db, "pm-3", sender_email="alice@example.com", body="z")
        emb = Embedder(
            db=db,
            model=mock_model,
            skip_senders=["notifications@github.com"],
            skip_domains=["amazon.co.uk"],
        )

        n = emb.mark_skipped_existing()

        assert n == 2
        rows = db.execute("SELECT pm_id, embedded FROM messages ORDER BY pm_id").fetchall()
        assert {r[0]: r[1] for r in rows} == {"pm-1": -1, "pm-2": -1, "pm-3": 0}

    def test_marks_glob_pattern_rows(self, db, mock_model):
        _insert_message(db, "pm-1", sender_email="noreply@stripe.com", body="x")
        _insert_message(db, "pm-2", sender_email="no-reply@github.com", body="y")
        _insert_message(db, "pm-3", sender_email="alice@example.com", body="z")
        emb = Embedder(db=db, model=mock_model, skip_senders=["noreply@*", "no-reply@*"])

        n = emb.mark_skipped_existing()

        assert n == 2
        embedded = {
            r[0]: r[1] for r in db.execute("SELECT pm_id, embedded FROM messages").fetchall()
        }
        assert embedded["pm-1"] == -1
        assert embedded["pm-2"] == -1
        assert embedded["pm-3"] == 0

    def test_idempotent(self, db, mock_model):
        _insert_message(db, "pm-1", sender_email="notifications@github.com", body="x")
        emb = Embedder(db=db, model=mock_model, skip_senders=["notifications@github.com"])

        first = emb.mark_skipped_existing()
        second = emb.mark_skipped_existing()

        assert first == 1
        # Second call finds nothing new (all already -1)
        assert second == 0

    def test_no_filters_does_nothing(self, db, mock_model):
        _insert_message(db, "pm-1", sender_email="alice@example.com", body="x")
        emb = Embedder(db=db, model=mock_model)

        n = emb.mark_skipped_existing()

        assert n == 0
        row = db.execute("SELECT embedded FROM messages WHERE pm_id = 'pm-1'").fetchone()
        assert row[0] == 0

    def test_does_not_disturb_already_embedded(self, db, mock_model):
        """A sender added to the skip list later shouldn't un-embed work already done."""
        emb = Embedder(db=db, model=mock_model, skip_senders=["notifications@github.com"])
        _insert_message(db, "pm-1", sender_email="notifications@github.com", body="x")
        # Pretend it was already embedded successfully before the filter was added
        db.execute("UPDATE messages SET embedded = 1 WHERE pm_id = 'pm-1'")
        db.commit()

        n = emb.mark_skipped_existing()

        assert n == 0
        row = db.execute("SELECT embedded FROM messages WHERE pm_id = 'pm-1'").fetchone()
        assert row[0] == 1


class TestUnembeddedQuery:
    def test_returns_unembedded_pm_ids(self, embedder, db):
        _insert_message(db, "pm-1", body="Hello")
        _insert_message(db, "pm-2", body="World")

        unembedded = embedder.get_unembedded(limit=10)
        assert len(unembedded) == 2

    def test_respects_priority_order(self, embedder, db):
        _insert_message(db, "pm-1", body="Archive msg")
        db.execute("UPDATE messages SET folder = 'Archive' WHERE pm_id = 'pm-1'")
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
