"""Email embedding pipeline for semantic vector search.

Encodes email content (sender + subject + body) into chunked vectors using
sentence-transformers, stores in sqlite-vec for similarity search.

Long emails are split into overlapping chunks, each prefixed with the
sender+subject header. Any matching chunk surfaces the parent message.

Downstream of body indexer: only embeds messages with body_indexed=1.
"""

from __future__ import annotations

import fnmatch
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
_API_BATCH_SIZE = 100  # Max texts per OpenAI-compatible API call
_HF_BATCH_SIZE = 16  # Conservative batch for HF Inference Providers


def _serialize_f32(vector: np.ndarray) -> bytes:
    """Serialize a float32 numpy array to bytes for sqlite-vec."""
    return struct.pack(f"{len(vector)}f", *vector)


_DEFAULT_MODEL = "intfloat/multilingual-e5-large-instruct"
_EMBEDDING_DIMS = 1024
_QUERY_PREFIX = "query: "

# Reranker input is capped hard: the cross-encoder cost scales with tokens, and
# the tail of an email is quoted replies, signatures and unsubscribe footers —
# noise for relevance. Subject + the opening lines carry the signal. Measured
# on the real corpus: 2000 chars cost ~21s per search, 300 chars ~3.6s, with
# no loss of the semantic matching bge-reranker-v2-m3 was picked for.
_RERANK_BODY_CHARS = 150
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
        hf_api_key: str = "",
        embedding_api_url: str = "",
        rerank_api_url: str = "",
        skip_senders: list[str] | None = None,
        skip_domains: list[str] | None = None,
        local_fallback: bool = True,
    ) -> None:
        self._db = db
        self._model_name = model_name
        self._together_key = api_key
        self._hf_key = hf_api_key
        self._embedding_api_url = embedding_api_url
        self._rerank_api_url = rerank_api_url
        self._local_model = model  # None = lazy-load on first search
        self._reranker = None  # Lazy-load on first search
        self._skip_senders = [s.lower() for s in (skip_senders or [])]
        self._skip_domains = [d.lower() for d in (skip_domains or [])]
        self._local_fallback = local_fallback
        self._ensure_table()

    def _should_skip(self, sender_email: str | None) -> bool:
        """Return True if this sender matches the configured skip list."""
        if not sender_email:
            return False
        addr = sender_email.lower()
        domain = addr.rpartition("@")[2]
        if domain and domain in self._skip_domains:
            return True
        for pattern in self._skip_senders:
            if "*" in pattern or "?" in pattern:
                if fnmatch.fnmatchcase(addr, pattern):
                    return True
            elif addr == pattern:
                return True
        return False

    @staticmethod
    def _load_local_model(model_name: str) -> Any:
        import logging

        logging.getLogger("sentence_transformers").setLevel(logging.WARNING)

        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(model_name, trust_remote_code=True)
        model.encode(["warmup"], show_progress_bar=False)
        return model

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        return isinstance(exc, RuntimeError) and "retryable" in str(exc)

    def _encode_via_hf(self, texts: list[str]) -> np.ndarray:
        """Encode texts via Hugging Face Inference Providers (TEI).

        Returns the raw feature-extraction vectors: a list of N 1024-float
        vectors for N inputs. Identical model to the local one, so the
        resulting vectors are interchangeable with the existing index.
        """
        import httpx
        from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

        @retry(
            retry=retry_if_exception(self._is_retryable),
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, max=10),
            before_sleep=lambda rs: logger.warning("embedder.hf_retry", attempt=rs.attempt_number),
        )
        def _call() -> np.ndarray:
            resp = httpx.post(
                self._embedding_api_url,
                headers={"Authorization": f"Bearer {self._hf_key}"},
                json={"inputs": texts, "options": {"wait_for_model": True}},
                timeout=120,
            )
            if resp.status_code in (429, 502, 503, 504):
                raise RuntimeError(f"retryable: {resp.status_code}")
            if resp.status_code != 200:
                raise RuntimeError(f"HF API {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            # Single input returns one flat vector; batch returns list of vectors.
            if data and isinstance(data[0], (int, float)):
                data = [data]
            return np.array(data, dtype=np.float32)

        return _call()

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
        """Bring both inference paths to a ready state.

        When the HF endpoints are configured they replace the local weights —
        no point loading ~4.6GB to sit idle — but the router cold-starts a
        model on first use (measured ~30s). So ping both endpoints here rather
        than making the first real search pay it.
        """
        use_hf_embed = bool(self._hf_key and self._embedding_api_url)
        use_hf_rerank = bool(self._hf_key and self._rerank_api_url)

        if use_hf_embed:
            try:
                self._encode_via_hf(["warmup"])
            except Exception:
                logger.warning("embedder.warmup.hf_embed_failed", exc_info=True)
        elif self._local_model is None:
            logger.info("embedder.warmup.embedding_model")
            self._local_model = self._load_local_model(self._model_name)

        if use_hf_rerank:
            try:
                self._rerank_via_hf([["warmup", "warmup"]])
            except Exception:
                logger.warning("embedder.warmup.hf_rerank_failed", exc_info=True)
        elif self._reranker is None:
            from sentence_transformers import CrossEncoder

            logger.info("embedder.warmup.reranker")
            self._reranker = CrossEncoder(_RERANKER_MODEL)

        logger.info("embedder.warmup.done", hf_embed=use_hf_embed, hf_rerank=use_hf_rerank)

    def _encode_local(self, texts: list[str]) -> np.ndarray:
        """Encode texts using local model. Lazy-loads on first call."""
        if self._local_model is None:
            logger.info("embedder.loading_local_model")
            self._local_model = self._load_local_model(self._model_name)
        return self._local_model.encode(texts, batch_size=_BATCH_SIZE, show_progress_bar=False)

    def _encode_query(self, texts: list[str]) -> np.ndarray:
        """Encode a search query, preferring the HF API over the local model.

        The local CPU model costs ~5s per query, which makes iterating on a
        search interactively unusable. The same model on HF returns the
        identical vector (cosine 1.0, float32 rounding only) in well under a
        second. Falls back to local so a search never fails outright.
        """
        if self._hf_key and self._embedding_api_url:
            try:
                return self._encode_via_hf(texts)
            except Exception:
                logger.warning("embedder.query_api_fallback_local", exc_info=True)
        return self._encode_local(texts)

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
        filtered_ids = []

        for pm_id in pm_ids:
            body = self._db.bodies.get(pm_id)
            if not body:
                skip_ids.append(pm_id)
                continue
            msg = self._db.messages.get(pm_id)
            if not msg:
                skip_ids.append(pm_id)
                continue
            if self._should_skip(msg.sender_email):
                filtered_ids.append(pm_id)
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

        # Filter-matched senders also get -1 so they aren't retried
        for pm_id in filtered_ids:
            self._db.execute(
                "UPDATE messages SET embedded = -1 WHERE pm_id = ?",
                [pm_id],
            )
        if filtered_ids:
            self._db.commit()
            logger.info("embedder.skipped_filter", count=len(filtered_ids))

        if not all_texts:
            return 0

        # Encode chunks (batched for API, unbatched for local)
        if use_api and (self._hf_key or self._together_key):
            try:
                # Send in sub-batches to stay within API limits
                encoder = self._encode_via_hf if self._hf_key else self._encode_via_api
                batch_size = _HF_BATCH_SIZE if self._hf_key else _API_BATCH_SIZE
                all_vectors = []
                for i in range(0, len(all_texts), batch_size):
                    batch = all_texts[i : i + batch_size]
                    all_vectors.append(encoder(batch))
                vectors = np.concatenate(all_vectors)
            except Exception as e:
                if self._local_fallback:
                    # Slow is acceptable; stalled is not. Local CPU encode
                    # keeps progress moving while the API outage clears.
                    logger.warning(
                        "embedder.api_fallback_local",
                        error=str(e),
                        chunks=len(all_texts),
                    )
                    vectors = self._encode_local(all_texts)
                else:
                    logger.warning(
                        "embedder.api_failed_skipping",
                        error=str(e),
                        chunks=len(all_texts),
                    )
                    return 0
        else:
            vectors = self._encode_local(all_texts)

        # Store each chunk vector.
        #
        # NOT "INSERT OR REPLACE": vec0 does not honour it. Re-inserting a
        # chunk_id that already exists raises "UNIQUE constraint failed on
        # message_vectors primary key" instead of replacing the row. Because a
        # run that fails partway leaves its earlier chunks behind, every retry
        # then collided with its own leftovers -- which is what wedged the embed
        # drain permanently at ~800 messages. Verifying on the live table:
        # INSERT OR REPLACE fails, DELETE + INSERT succeeds.
        for chunk_id, vec in zip(all_chunk_ids, vectors):
            vec_f32 = np.asarray(vec, dtype=np.float32)
            self._db.execute("DELETE FROM message_vectors WHERE chunk_id = ?", [chunk_id])
            self._db.execute(
                "INSERT INTO message_vectors (chunk_id, embedding) VALUES (?, ?)",
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
        vec = self._encode_query([f"{_QUERY_PREFIX}{query}"])
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
        vec = self._encode_query([f"{_QUERY_PREFIX}{query}"])
        query_vec = np.asarray(vec[0], dtype=np.float32)

        # Over-fetch chunks to account for dedup + filtering. Kept modest:
        # vec0 is brute-force, so k directly drives latency on a cold cache.
        k = limit * 4

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
            doc = f"From: {sender}\nSubject: {msg.subject or ''}\n\n{body[:_RERANK_BODY_CHARS]}"
            pairs.append([query, doc])
        return pairs

    def _rerank_via_hf(self, pairs: list[list[str]]) -> np.ndarray:
        """Score (query, document) pairs via the HF text-ranking endpoint.

        Identical BAAI/bge-reranker-v2-m3 weights to the local cross-encoder,
        but ~0.3s for a whole candidate set instead of ~8s per candidate on
        four CPU threads. Returns one score per pair, in input order.
        """
        import httpx
        from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

        @retry(
            retry=retry_if_exception(self._is_retryable),
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, max=10),
            before_sleep=lambda rs: logger.warning(
                "embedder.rerank_retry", attempt=rs.attempt_number
            ),
        )
        def _call() -> np.ndarray:
            resp = httpx.post(
                self._rerank_api_url,
                headers={"Authorization": f"Bearer {self._hf_key}"},
                json={"inputs": pairs},
                timeout=120,
            )
            if resp.status_code in (429, 502, 503, 504):
                raise RuntimeError(f"retryable: {resp.status_code}")
            if resp.status_code != 200:
                raise RuntimeError(f"HF rerank {resp.status_code}: {resp.text[:200]}")
            scores = resp.json()["scores"]
            # A single pair comes back as a bare float rather than a list.
            if isinstance(scores, (int, float)):
                scores = [scores]
            return np.array(scores, dtype=np.float32)

        return _call()

    def score(self, query: str, candidates: list, db: Any) -> list[tuple[float, Any]]:
        """Score candidates using cross-encoder. Returns (score, msg) sorted by score desc."""
        if not candidates:
            return []
        pairs = self._build_pairs(query, candidates, db)

        if self._hf_key and self._rerank_api_url:
            try:
                scores = self._rerank_via_hf(pairs)
                return sorted(zip(scores, candidates), key=lambda x: x[0], reverse=True)
            except Exception:
                # Deliberately NOT falling back to the local cross-encoder:
                # it costs minutes per search, which is far worse than keeping
                # the recall order. Ordering degrades, the search still returns.
                logger.warning("embedder.rerank_fallback_order", exc_info=True)
                return [(float(len(candidates) - i), m) for i, m in enumerate(candidates)]

        self._ensure_reranker()
        assert self._reranker is not None
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

    def mark_skipped_existing(self) -> int:
        """One-shot: mark all currently-unembedded messages whose sender matches
        the skip list as embedded=-1, dropping them from the embed backlog.

        Only touches rows with embedded=0 — work already done isn't undone.
        Returns the number of rows updated.
        """
        if not self._skip_senders and not self._skip_domains:
            return 0

        clauses: list[str] = []
        params: list[str] = []

        for pattern in self._skip_senders:
            if "*" in pattern or "?" in pattern:
                clauses.append("LOWER(sender_email) GLOB ?")
                params.append(pattern.lower())
            else:
                clauses.append("LOWER(sender_email) = ?")
                params.append(pattern.lower())

        for domain in self._skip_domains:
            # Use a glob so we don't false-match suffixes like "evil-amazon.co.uk"
            clauses.append("LOWER(sender_email) GLOB ?")
            params.append(f"*@{domain.lower()}")

        sql = (
            "UPDATE messages SET embedded = -1"
            " WHERE embedded = 0 AND (" + " OR ".join(clauses) + ")"
        )
        cur = self._db.execute(sql, params)
        self._db.commit()
        n = cur.rowcount or 0
        if n:
            logger.info("embedder.mark_skipped_existing", count=n)
        return n

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
