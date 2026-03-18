"""Email embedding pipeline for semantic vector search.

Encodes email content (sender + subject + body) into vectors using
sentence-transformers, stores in sqlite-vec for similarity search.

Downstream of body indexer: only embeds messages with body_indexed=1.
"""

from __future__ import annotations

import struct
from typing import Any

import numpy as np
import structlog

from email_mcp.db import Database

logger = structlog.get_logger(__name__)

_BATCH_SIZE = 64
_MAX_BODY_CHARS = 2000


def _serialize_f32(vector: np.ndarray) -> bytes:
    """Serialize a float32 numpy array to bytes for sqlite-vec."""
    return struct.pack(f"{len(vector)}f", *vector)


_DEFAULT_MODEL = "nomic-ai/nomic-embed-text-v1.5"
_EMBEDDING_DIMS = 768
_QUERY_PREFIX = "search_query: "
_DOC_PREFIX = "search_document: "


class Embedder:
    """Embed email content and search by vector similarity."""

    def __init__(
        self,
        db: Database,
        model: Any = None,
        model_name: str = _DEFAULT_MODEL,
    ) -> None:
        self._db = db
        self._ensure_table()
        if model is not None:
            self._model = model
        else:
            import logging
            import os

            # Suppress noisy model load reports and tqdm progress bars
            logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
            os.environ["TOKENIZERS_PARALLELISM"] = "false"

            # Let PyTorch use multiple cores for inference
            import torch

            torch.set_num_threads(min(4, os.cpu_count() or 1))

            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(model_name, trust_remote_code=True)
            self._model.encode(["warmup"], show_progress_bar=False)

    def _ensure_table(self) -> None:
        """Create the vectors table if it doesn't exist."""
        import sqlite_vec

        self._db._conn.enable_load_extension(True)
        sqlite_vec.load(self._db._conn)
        self._db._conn.enable_load_extension(False)

        self._db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS message_vectors"
            f" USING vec0(pm_id TEXT PRIMARY KEY, embedding float[{_EMBEDDING_DIMS}])"
        )
        # Add embedded column if missing
        existing = {row[1] for row in self._db.execute("PRAGMA table_info(messages)").fetchall()}
        if "embedded" not in existing:
            self._db.execute("ALTER TABLE messages ADD COLUMN embedded INTEGER NOT NULL DEFAULT 0")
            self._db.commit()

    def embed_batch(self, pm_ids: list[str]) -> int:
        """Embed a batch of messages. Returns count of successfully embedded."""
        texts = []
        valid_ids = []

        for pm_id in pm_ids:
            body = self._db.bodies.get(pm_id)
            if not body:
                continue
            msg = self._db.messages.get(pm_id)
            if not msg:
                continue

            text = (
                f"{_DOC_PREFIX}"
                f"From: {msg.sender_name or ''}"
                f" <{msg.sender_email or ''}>\n"
                f"Subject: {msg.subject or ''}\n\n"
                f"{body[:_MAX_BODY_CHARS]}"
            )
            texts.append(text)
            valid_ids.append(pm_id)

        if not texts:
            return 0

        vectors = self._model.encode(
            texts, batch_size=_BATCH_SIZE, show_progress_bar=False
        )

        for pm_id, vec in zip(valid_ids, vectors):
            vec_f32 = np.asarray(vec, dtype=np.float32)
            self._db.execute(
                "INSERT OR REPLACE INTO message_vectors (pm_id, embedding) VALUES (?, ?)",
                [pm_id, _serialize_f32(vec_f32)],
            )
            self._db.execute(
                "UPDATE messages SET embedded = 1 WHERE pm_id = ?",
                [pm_id],
            )
        self._db.commit()
        return len(valid_ids)

    def search(self, query: str, limit: int = 20) -> list[str]:
        """Semantic search. Returns pm_ids ranked by similarity."""
        vec = self._model.encode(
            [f"{_QUERY_PREFIX}{query}"], batch_size=1, show_progress_bar=False
        )
        query_vec = np.asarray(vec[0], dtype=np.float32)

        rows = self._db.execute(
            "SELECT pm_id, distance FROM message_vectors"
            " WHERE embedding MATCH ? AND k = ?"
            " ORDER BY distance",
            [_serialize_f32(query_vec), limit],
        ).fetchall()

        return [r[0] for r in rows]

    def search_with_filters(
        self,
        query: str,
        where_clause: str = "1",
        params: list[Any] | None = None,
        limit: int = 20,
    ) -> list[str]:
        """Semantic search with SQL pre-filters.

        Args:
            query: Natural language query to embed.
            where_clause: SQL WHERE clause for pre-filtering (e.g. "folder = ?").
            params: Parameters for the WHERE clause.
            limit: Max results.
        """
        # sqlite-vec requires k=? on the vec0 table directly.
        # Do vector search first (over-fetch), then post-filter with SQL.
        vec = self._model.encode(
            [f"{_QUERY_PREFIX}{query}"], batch_size=1, show_progress_bar=False
        )
        query_vec = np.asarray(vec[0], dtype=np.float32)

        # Over-fetch to account for filtering
        k = limit * 5

        vector_rows = self._db.execute(
            "SELECT pm_id, distance FROM message_vectors"
            " WHERE embedding MATCH ? AND k = ?"
            " ORDER BY distance",
            [_serialize_f32(query_vec), k],
        ).fetchall()

        if where_clause == "1" and not params:
            return [r[0] for r in vector_rows[:limit]]

        # Post-filter with the WHERE clause
        candidate_ids = [r[0] for r in vector_rows]
        if not candidate_ids:
            return []

        placeholders = ",".join("?" * len(candidate_ids))
        # Use alias 'm' to match caller's where_clause (e.g. "m.folder = ?")
        sql = (
            f"SELECT m.pm_id FROM messages m"
            f" WHERE m.pm_id IN ({placeholders})"
            f" AND {where_clause}"
        )
        filtered = self._db.execute(
            sql, [*candidate_ids, *(params or [])]
        ).fetchall()
        filtered_ids = {r[0] for r in filtered}

        # Preserve vector distance ordering
        return [
            r[0] for r in vector_rows if r[0] in filtered_ids
        ][:limit]

    def get_unembedded(self, limit: int = 1000) -> list[str]:
        """Get pm_ids that have bodies but aren't embedded yet.

        Returns in priority order: INBOX first, then other folders,
        then NULL folder last.
        """
        rows = self._db.execute(
            "SELECT pm_id FROM messages"
            " WHERE body_indexed = 1 AND embedded = 0"
            " ORDER BY"
            "   CASE"
            "     WHEN folder = 'INBOX' THEN 0"
            "     WHEN folder = 'Sent' THEN 1"
            "     WHEN folder = 'Drafts' THEN 2"
            "     WHEN folder IS NOT NULL THEN 3"
            "     ELSE 4"
            "   END,"
            "   date DESC"
            " LIMIT ?",
            [limit],
        ).fetchall()
        return [r[0] for r in rows]
