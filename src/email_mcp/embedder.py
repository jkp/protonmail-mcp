"""Email embedding pipeline for semantic vector search.

Encodes email content (sender + subject + body) into chunked vectors using
sentence-transformers, stores in sqlite-vec for similarity search.

Long emails are split into overlapping chunks, each prefixed with the
sender+subject header. Any matching chunk surfaces the parent message.

Downstream of body indexer: only embeds messages with body_indexed=1.
"""

from __future__ import annotations

import os
import struct
from typing import Any

import numpy as np
import structlog

from email_mcp.convert import body_for_display
from email_mcp.db import Database

logger = structlog.get_logger(__name__)

_BATCH_SIZE = 64
# Chunk size in chars (~200 tokens). With header (~40 tokens) and prefix (~10 tokens),
# total stays under the 512-token model limit.
_CHUNK_CHARS = 400
_CHUNK_OVERLAP = 100  # Overlap between chunks to avoid splitting mid-sentence
_MAX_CHUNKS_PER_MSG = 5  # Cap chunks — first few have the most signal
_API_BATCH_SIZE = 100  # Max texts per Together API call


def _serialize_f32(vector: np.ndarray) -> bytes:
    """Serialize a float32 numpy array to bytes for sqlite-vec."""
    return struct.pack(f"{len(vector)}f", *vector)


_DEFAULT_MODEL = "intfloat/multilingual-e5-large-instruct"
_EMBEDDING_DIMS = 1024
_QUERY_PREFIX = "query: "
_DOC_PREFIX = "passage: "
_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"


from email_reply_parser import EmailReplyParser  # noqa: E402


def _strip_quotes(body: str) -> str:
    """Extract only the new content from an email, stripping quoted replies.

    Uses email-reply-parser (GitHub's production library) to robustly
    handle "> " quotes, "On X wrote:" headers, forwarded blocks, and
    Outlook-style quote markers.
    """
    return EmailReplyParser.parse_reply(body)


def _make_chunks(full_text: str) -> list[str]:
    """Split text into overlapping chunks for embedding.

    Returns list of texts ready for embedding (with _DOC_PREFIX).
    Short texts produce a single chunk.
    """
    if len(full_text) <= _CHUNK_CHARS:
        return [f"{_DOC_PREFIX}{full_text}"]

    chunks = []
    step = _CHUNK_CHARS - _CHUNK_OVERLAP
    pos = 0
    while pos < len(full_text):
        chunk = full_text[pos : pos + _CHUNK_CHARS]
        chunks.append(f"{_DOC_PREFIX}{chunk}")
        pos += step
        if pos + _CHUNK_OVERLAP >= len(full_text):
            break

    return chunks[:_MAX_CHUNKS_PER_MSG] or [f"{_DOC_PREFIX}{full_text}"]


class Embedder:
    """Embed email content and search by vector similarity.

    Uses Together API for batch embedding (fast backfill) when
    TOGETHER_API_KEY is set. Falls back to local model for
    single-query inference (search) and when no API key is available.
    """

    def __init__(
        self,
        db: Database,
        model: Any = None,
        model_name: str = _DEFAULT_MODEL,
        api_key: str = "",
    ) -> None:
        self._db = db
        self._model_name = model_name
        self._together_key = api_key
        self._local_model = model  # None = lazy-load on first search
        self._reranker = None  # Lazy-load on first search
        self._ensure_table()

    @staticmethod
    def _load_local_model(model_name: str) -> Any:
        import logging

        logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
        os.environ["TOKENIZERS_PARALLELISM"] = "false"

        import torch

        torch.set_num_threads(min(4, os.cpu_count() or 1))

        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(model_name, trust_remote_code=True)
        model.encode(["warmup"], show_progress_bar=False)
        return model

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        return isinstance(exc, RuntimeError) and "retryable" in str(exc)

    def _encode_via_api(self, texts: list[str]) -> np.ndarray:
        """Encode texts using Together API with retry on transient errors."""
        import httpx
        from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

        @retry(
            retry=retry_if_exception(self._is_retryable),
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, max=10),
            before_sleep=lambda rs: logger.warning("embedder.api_retry", attempt=rs.attempt_number),
        )
        def _call() -> np.ndarray:
            resp = httpx.post(
                "https://api.together.xyz/v1/embeddings",
                headers={"Authorization": f"Bearer {self._together_key}"},
                json={"model": self._model_name, "input": texts},
                timeout=60,
            )
            if resp.status_code in (500, 502, 503, 429):
                raise RuntimeError(f"retryable: {resp.status_code}")
            if resp.status_code != 200:
                detail = resp.json().get("error", {}).get("message", resp.text[:200])
                raise RuntimeError(f"Together API: {detail}")
            data = resp.json()
            return np.array([d["embedding"] for d in data["data"]], dtype=np.float32)

        return _call()

    def warmup(self) -> None:
        """Pre-load both the embedding model and reranker.

        Called from a background task at startup so the first search
        doesn't pay the ~10s loading cost.
        """
        from sentence_transformers import CrossEncoder

        if self._local_model is None:
            logger.info("embedder.warmup.embedding_model")
            self._local_model = self._load_local_model(self._model_name)
        if self._reranker is None:
            logger.info("embedder.warmup.reranker")
            self._reranker = CrossEncoder(_RERANKER_MODEL)
        logger.info("embedder.warmup.done")

    def _encode_local(self, texts: list[str]) -> np.ndarray:
        """Encode texts using local model. Lazy-loads on first call."""
        if self._local_model is None:
            logger.info("embedder.loading_local_model")
            self._local_model = self._load_local_model(self._model_name)
        return self._local_model.encode(texts, batch_size=_BATCH_SIZE, show_progress_bar=False)

    def _ensure_table(self) -> None:
        """Create the vectors table if it doesn't exist."""
        import sqlite_vec

        self._db._conn.enable_load_extension(True)
        sqlite_vec.load(self._db._conn)
        self._db._conn.enable_load_extension(False)

        # Recreate table if it has the old pm_id-only schema
        # New schema uses chunk_id (pm_id:chunk_index) as primary key
        try:
            cols = self._db.execute(
                "SELECT name FROM pragma_table_info('message_vectors')"
            ).fetchall()
            col_names = {r[0] for r in cols}
            if "pm_id" in col_names and "chunk_id" not in col_names:
                self._db.execute("DROP TABLE message_vectors")
                self._db.commit()
                logger.info("embedder.table_migrated", reason="pm_id→chunk_id")
        except Exception:
            pass

        self._db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS message_vectors"
            f" USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[{_EMBEDDING_DIMS}])"
        )
        # Add embedded column if missing
        existing = {row[1] for row in self._db.execute("PRAGMA table_info(messages)").fetchall()}
        if "embedded" not in existing:
            self._db.execute("ALTER TABLE messages ADD COLUMN embedded INTEGER NOT NULL DEFAULT 0")
            self._db.commit()

    def embed_batch(self, pm_ids: list[str], use_api: bool = False) -> int:
        """Embed a batch of messages. Returns count of successfully embedded.

        Args:
            pm_ids: Messages to embed.
            use_api: Use Together API for encoding. Only for bulk backfill —
                     ongoing trickle should use local model to avoid costs.
        """
        all_texts = []
        all_chunk_ids = []
        skip_ids = []

        for pm_id in pm_ids:
            body = self._db.bodies.get(pm_id)
            if not body:
                skip_ids.append(pm_id)
                continue
            msg = self._db.messages.get(pm_id)
            if not msg:
                skip_ids.append(pm_id)
                continue

            sender = msg.sender_name or msg.sender_email
            subject = msg.subject or ""
            # Cap body before HTML conversion — multi-MB bodies hang justhtml.
            # 10K chars is plenty; we only embed the first few chunks anyway.
            plain = body_for_display(body[:10_000])
            new_content = _strip_quotes(plain)
            full_text = f"From: {sender}\nSubject: {subject}\n\n{new_content}"

            chunks = _make_chunks(full_text)
            for i, chunk_text in enumerate(chunks):
                all_texts.append(chunk_text)
                all_chunk_ids.append(f"{pm_id}:{i}")

        # Mark skipped messages as -1 (nothing to embed)
        for pm_id in skip_ids:
            self._db.execute(
                "UPDATE messages SET embedded = -1 WHERE pm_id = ?",
                [pm_id],
            )
        if skip_ids:
            self._db.commit()
            logger.info("embedder.skipped_empty", count=len(skip_ids))

        if not all_texts:
            return 0

        # Encode chunks (batched for API, unbatched for local)
        if use_api and self._together_key:
            try:
                # Send in sub-batches to stay within API limits
                all_vectors = []
                for i in range(0, len(all_texts), _API_BATCH_SIZE):
                    batch = all_texts[i : i + _API_BATCH_SIZE]
                    all_vectors.append(self._encode_via_api(batch))
                vectors = np.concatenate(all_vectors)
            except Exception as e:
                # Don't fall back to local for large batches — it'll
                # grind the CPU for ages. Just skip and retry next cycle.
                logger.warning(
                    "embedder.api_failed_skipping",
                    error=str(e),
                    chunks=len(all_texts),
                )
                return 0
        else:
            vectors = self._encode_local(all_texts)

        # Store each chunk vector
        for chunk_id, vec in zip(all_chunk_ids, vectors):
            vec_f32 = np.asarray(vec, dtype=np.float32)
            self._db.execute(
                "INSERT OR REPLACE INTO message_vectors (chunk_id, embedding) VALUES (?, ?)",
                [chunk_id, _serialize_f32(vec_f32)],
            )

        # Mark messages as embedded
        embedded_pm_ids = {cid.rsplit(":", 1)[0] for cid in all_chunk_ids}
        for pm_id in embedded_pm_ids:
            self._db.execute(
                "UPDATE messages SET embedded = 1 WHERE pm_id = ?",
                [pm_id],
            )
        self._db.commit()
        return len(embedded_pm_ids)

    def search(self, query: str, limit: int = 20) -> list[str]:
        """Semantic search. Returns pm_ids ranked by best chunk distance.

        No distance threshold — the reranker + bulk penalty handle precision.
        Vector search's job is candidate recall: cast a wide net.
        """
        vec = self._encode_local([f"{_QUERY_PREFIX}{query}"])
        query_vec = np.asarray(vec[0], dtype=np.float32)

        # Over-fetch chunks since multiple chunks can belong to one message
        rows = self._db.execute(
            "SELECT chunk_id, distance FROM message_vectors"
            " WHERE embedding MATCH ? AND k = ?"
            " ORDER BY distance",
            [_serialize_f32(query_vec), limit * 3],
        ).fetchall()

        if not rows:
            return []

        logger.info(
            "embedder.search.distances",
            query=query,
            top=f"{rows[0][1]:.3f}",
            bottom=f"{rows[-1][1]:.3f}",
        )

        seen: set[str] = set()
        result: list[str] = []
        for chunk_id, _dist in rows:
            pm_id = chunk_id.rsplit(":", 1)[0]
            if pm_id not in seen:
                seen.add(pm_id)
                result.append(pm_id)
                if len(result) >= limit:
                    break
        return result

    def search_with_filters(
        self,
        query: str,
        where_clause: str = "1",
        params: list[Any] | None = None,
        limit: int = 20,
    ) -> list[str]:
        """Semantic search with SQL pre-filters."""
        vec = self._encode_local([f"{_QUERY_PREFIX}{query}"])
        query_vec = np.asarray(vec[0], dtype=np.float32)

        # Over-fetch chunks to account for dedup + filtering
        k = limit * 10

        vector_rows = self._db.execute(
            "SELECT chunk_id, distance FROM message_vectors"
            " WHERE embedding MATCH ? AND k = ?"
            " ORDER BY distance",
            [_serialize_f32(query_vec), k],
        ).fetchall()

        # Deduplicate chunks → pm_ids (no distance threshold — reranker handles precision)
        seen: set[str] = set()
        candidate_ids: list[str] = []
        for chunk_id, _dist in vector_rows:
            pm_id = chunk_id.rsplit(":", 1)[0]
            if pm_id not in seen:
                seen.add(pm_id)
                candidate_ids.append(pm_id)

        if not candidate_ids:
            return []

        if where_clause == "1" and not params:
            return candidate_ids[:limit]

        # Post-filter with the WHERE clause
        placeholders = ",".join("?" * len(candidate_ids))
        sql = f"SELECT m.pm_id FROM messages m WHERE m.pm_id IN ({placeholders}) AND {where_clause}"
        filtered = self._db.execute(sql, [*candidate_ids, *(params or [])]).fetchall()
        filtered_ids = {r[0] for r in filtered}

        # Preserve vector distance ordering
        return [pid for pid in candidate_ids if pid in filtered_ids][:limit]

    def _ensure_reranker(self) -> None:
        if self._reranker is None:
            from sentence_transformers import CrossEncoder

            logger.info("embedder.loading_reranker")
            self._reranker = CrossEncoder(_RERANKER_MODEL)

    def _build_pairs(self, query: str, results: list, db: Any) -> list[list[str]]:
        pairs = []
        for msg in results:
            body = db.bodies.get(msg.pm_id) or ""
            sender = msg.sender_name or msg.sender_email
            doc = f"From: {sender}\nSubject: {msg.subject or ''}\n\n{body[:2000]}"
            pairs.append([query, doc])
        return pairs

    def score(self, query: str, candidates: list, db: Any) -> list[tuple[float, Any]]:
        """Score candidates using cross-encoder. Returns (score, msg) sorted by score desc."""
        if not candidates:
            return []
        self._ensure_reranker()
        assert self._reranker is not None
        pairs = self._build_pairs(query, candidates, db)
        scores = self._reranker.predict(pairs)
        return sorted(zip(scores, candidates), key=lambda x: x[0], reverse=True)

    def rerank(self, query: str, results: list, db: Any) -> list:
        """Rerank search results using a cross-encoder model.

        Takes the candidate MessageRows, builds (query, document) pairs,
        scores them, and returns sorted by relevance.
        """
        if not results:
            return results
        ranked = self.score(query, results, db)
        logger.info(
            "embedder.reranked",
            count=len(ranked),
            top_score=f"{ranked[0][0]:.2f}" if ranked else None,
        )
        return [msg for _, msg in ranked]

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
