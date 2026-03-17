# email-mcp

Maildir-native MCP server for email with full-text search and IMAP-first mutations.

## Quick Start

```bash
uv sync                    # install deps
uv run pytest              # run tests
uv run email-mcp           # start server (stdio)
```

## Architecture (v3)

- **Reads are local** — Maildir files + notmuch index, no network round-trips
- **Mutations are IMAP-first** — move/archive/delete/flags go directly to IMAP (authoritative)
- **Optimistic local updates** — after IMAP success, local Maildir is updated immediately
- **Tiered sync** — INBOX: IDLE + 60s periodic (0.3s). Everything else: nightly
- **Debounced reindex** — notmuch new runs at most once per 60s after mutations
- **Message-ID** identifies emails (not IMAP UIDs)
- **FastMCP** server with 14 tools, stdio or HTTP transport

### Critical invariants (see [docs/data-flow-invariants.md](docs/data-flow-invariants.md))

- **All mutations MUST go through IMAP.** Never modify Maildir files directly to mutate state -- it creates split-brain between IMAP server, mbsync, and notmuch.
- **notmuch is a search accelerator, not a source of truth.** Folder info derived from file paths can be stale. Handle misses gracefully.
- **Test cleanup MUST use IMAP path** (`search_and_delete`), not direct file deletion.

## Key Modules

| Module | Purpose |
|--------|---------|
| `server.py` | FastMCP instance, lifespan (IMAP/sync/IDLE init), entry point |
| `config.py` | Pydantic settings from env vars (EMAIL_MCP_ prefix) |
| `store.py` | Maildir ops: read, list, move, delete, flags, optimistic_move |
| `search.py` | notmuch search + luqum Gmail query translation |
| `imap.py` | IMAP mutator: COPY/STORE/EXPUNGE by Message-ID |
| `idle.py` | IMAP IDLE listener for real-time INBOX notifications |
| `sync.py` | SyncEngine: per-folder mbsync, singleton, debounced notmuch, nightly |
| `composer.py` | Reply/forward/new message construction (stdlib email) |
| `sender.py` | aiosmtplib SMTP wrapper |
| `convert.py` | HTML → markdown via html2text |
| `models.py` | Pydantic models (Email, Folder, Attachment, SearchResult, SyncStatus) |
| `tools/` | Tool implementations (listing, reading, searching, composing, managing) |

## Testing

Tests use `pytest-asyncio` with `asyncio_mode = "auto"`.
Store tests run against temporary Maildir fixtures (real files, no mocks).
IMAP/SMTP operations are mocked via `unittest.mock`.

```bash
uv run pytest -v            # verbose
uv run pytest --cov         # with coverage
```

### No sleep-based assertions

**Never use `time.sleep()` or `asyncio.sleep(N)` (where N > 0) to wait for async behavior in tests.** These make tests slow, flaky, and timing-dependent.

Instead, use event-based synchronization:

- **Debounce/timer tests:** Set debounce to 0, wrap the target method to signal an `asyncio.Event` on completion, then `await asyncio.wait_for(event.wait(), timeout=1.0)`.
- **Periodic loop tests:** Patch the engine's `_sleep` method with a noop that yields (`await asyncio.sleep(0)`), and cancel the loop from inside after N iterations.
- **Thread-blocking tests (e.g. IDLE):** Use `threading.Event` gates instead of `time.sleep()`. The mock blocks on `gate.wait()`, and the test calls `gate.set()` before stopping — the thread releases immediately.
- **Timer reset tests:** Inspect `handle.cancelled()` directly rather than sleeping past the timer.

`await asyncio.sleep(0)` is fine — it yields to the event loop without wall-clock delay.

## Configuration

All env vars use `EMAIL_MCP_` prefix:
- `EMAIL_MCP_IMAP_HOST`, `EMAIL_MCP_IMAP_PORT`, `EMAIL_MCP_IMAP_USERNAME`, `EMAIL_MCP_IMAP_PASSWORD`
- `EMAIL_MCP_IMAP_STARTTLS`, `EMAIL_MCP_IMAP_CERT_PATH`
- `EMAIL_MCP_SMTP_HOST`, `EMAIL_MCP_SMTP_PORT`, `EMAIL_MCP_SMTP_USERNAME`, `EMAIL_MCP_SMTP_PASSWORD`
- `EMAIL_MCP_MAILDIR_ROOT`, `EMAIL_MCP_FROM_NAME`, `EMAIL_MCP_FROM_ADDRESS`
- `EMAIL_MCP_TRANSPORT` (stdio/http), `EMAIL_MCP_HOST`, `EMAIL_MCP_PORT`
- `EMAIL_MCP_NOTMUCH_BIN`, `EMAIL_MCP_MBSYNC_BIN`, `EMAIL_MCP_MBSYNC_CHANNEL`
- `EMAIL_MCP_INBOX_SYNC_INTERVAL`, `EMAIL_MCP_NIGHTLY_SYNC_HOUR`, `EMAIL_MCP_NIGHTLY_SYNC_ENABLED`
- `EMAIL_MCP_IDLE_ENABLED`, `EMAIL_MCP_REINDEX_DEBOUNCE`

## Prerequisites (external)

- `mbsync` (isync) for IMAP ↔ Maildir sync
- `notmuch` for full-text search + indexing
- IMAP server (e.g. Protonmail Bridge on localhost)
