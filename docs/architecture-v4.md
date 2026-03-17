# email-mcp: Architecture v4

## Motivation

v3 has a fundamental design problem: two sources of truth. IMAP is authoritative
but mutations there cause Bridge to regenerate UIDs (UIDVALIDITY), which breaks
mbsync, which leaves notmuch stale, which causes "not found" failures that loop
forever. The system drifts easily and recovery requires manual intervention.

The root cause: we use IMAP COPY+DELETE to simulate what ProtonMail's native API
does in one atomic label-swap call. We use mbsync to simulate what ProtonMail's
own event loop already provides. We're fighting the abstraction layer.

v4 goes one level deeper: use ProtonMail's native HTTP API for metadata and
mutations, the event loop for real-time sync, and Bridge IMAP only for the one
thing we can't get elsewhere — decrypted message bodies.

## Design Principles

1. **One source of truth.** ProtonMail's servers own all state. SQLite is a
   local read cache, maintained by the event loop.
2. **Event-driven, not poll-driven.** Changes arrive via the ProtonMail event
   loop. No mbsync, no periodic folder scans, no UIDVALIDITY.
3. **Stable IDs forever.** ProtonMail message UUIDs never change. No IMAP UID
   mapping, no bookkeeping.
4. **IMAP is read-only, one-time.** Bridge is used only to fetch decrypted body
   text once per message. Never for mutations, never for sync state.
5. **Mutations are atomic.** Archive/move/delete are label-swap API calls.
   One round-trip, no multi-step COPY+DELETE sequences.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      MCP Clients                             │
└────────────────────────┬────────────────────────────────────┘
                         │ MCP (stdio / HTTP)
┌────────────────────────▼────────────────────────────────────┐
│                    email-mcp server                          │
│                                                              │
│  ┌──────────────┐  ┌────────────────┐  ┌─────────────────┐  │
│  │  MCP Tools   │  │  ProtonMail    │  │  SMTP Sender    │  │
│  │              │  │  API Client    │  │  (unchanged)    │  │
│  │  list/read   │  │                │  │                 │  │
│  │  search      │  │  mutations:    │  │  aiosmtplib     │  │
│  │  archive     │  │  label swap    │  │                 │  │
│  │  delete      │  │  event loop    │  │                 │  │
│  │  compose     │  │  label list    │  │                 │  │
│  └──────┬───────┘  └───────┬────────┘  └─────────────────┘  │
│         │                  │                                  │
│  ┌──────▼──────────────────▼──────────────────────────────┐  │
│  │                  SQLite Database                        │  │
│  │                                                         │  │
│  │  messages     — metadata (pm_id, subject, folder, ...)  │  │
│  │  message_bodies — decrypted plaintext (body indexed)    │  │
│  │  fts_bodies   — FTS5 full-text index over bodies        │  │
│  │  labels       — folder/label definitions                 │  │
│  │  sync_state   — last_event_id, initial_sync flags       │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │               Body Indexer (background)                  │  │
│  │                                                          │  │
│  │  Queue of pm_ids needing body fetch                      │  │
│  │  IMAP FETCH (Bridge) → decrypt → store → FTS index      │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                              │
└──────────────────────────────────────────────────────────────┘
         │ REST API                              │ IMAP (body only)
┌────────▼──────────────┐            ┌──────────▼──────────────┐
│  ProtonMail Servers   │            │  ProtonMail Bridge       │
│                       │            │  localhost:1143           │
│  /mail/v4/events      │            │                          │
│  /mail/v4/messages    │            │  FETCH body[TEXT]        │
│  /mail/v4/labels      │            │  (decrypts PGP)          │
│  /auth                │            │  one-time per message    │
└───────────────────────┘            └──────────────────────────┘
```

## What Changes vs v3

| v3 | v4 |
|----|----|
| mbsync → Maildir → notmuch | ProtonMail event loop → SQLite FTS5 |
| IMAP COPY+DELETE mutations | ProtonMail label API (atomic) |
| UIDVALIDITY / mbsync drift | Stable PM UUIDs, no sync state |
| IMAP IDLE on INBOX | Event loop covers all folders |
| Debounced notmuch reindex | Event-driven SQLite updates |
| notmuch Gmail query translation | SQLite FTS5 + WHERE clauses |
| Maildir files as store | SQLite rows |

## What Stays

| Component | Why |
|-----------|-----|
| SMTP sender | Unchanged — sending works fine |
| Composer | Unchanged |
| HTML → markdown | Unchanged |
| MCP tool signatures | Same from the AI's perspective |
| Bridge IMAP | Body fetch only, existing code reused |

## Data Model

### messages

```sql
CREATE TABLE messages (
    pm_id        TEXT PRIMARY KEY,   -- ProtonMail UUID (never changes)
    message_id   TEXT UNIQUE,        -- RFC 2822 Message-ID (IMAP correlation)
    subject      TEXT,
    sender_name  TEXT,
    sender_email TEXT,
    recipients   TEXT,               -- JSON: [{name, email}, ...]
    date         INTEGER NOT NULL,   -- Unix timestamp
    unread       INTEGER NOT NULL DEFAULT 1,
    label_ids    TEXT NOT NULL DEFAULT '[]',  -- JSON array of label IDs
    folder       TEXT,               -- Derived: exclusive folder label name
    size         INTEGER,
    has_attachments INTEGER NOT NULL DEFAULT 0,
    body_indexed INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE INDEX idx_messages_folder ON messages(folder);
CREATE INDEX idx_messages_date   ON messages(date DESC);
CREATE INDEX idx_messages_unread ON messages(unread) WHERE unread = 1;
```

### message_bodies + FTS5

```sql
CREATE TABLE message_bodies (
    rowid  INTEGER PRIMARY KEY,
    pm_id  TEXT NOT NULL UNIQUE REFERENCES messages(pm_id) ON DELETE CASCADE,
    body   TEXT NOT NULL
);

-- Full-text index backed by message_bodies
CREATE VIRTUAL TABLE fts_bodies USING fts5(
    pm_id UNINDEXED,
    body,
    content='message_bodies',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

-- Keep FTS in sync with message_bodies
CREATE TRIGGER bodies_ai AFTER INSERT ON message_bodies BEGIN
    INSERT INTO fts_bodies(rowid, pm_id, body) VALUES (new.rowid, new.pm_id, new.body);
END;
CREATE TRIGGER bodies_ad AFTER DELETE ON message_bodies BEGIN
    INSERT INTO fts_bodies(fts_bodies, rowid, pm_id, body)
    VALUES ('delete', old.rowid, old.pm_id, old.body);
END;
CREATE TRIGGER bodies_au AFTER UPDATE ON message_bodies BEGIN
    INSERT INTO fts_bodies(fts_bodies, rowid, pm_id, body)
    VALUES ('delete', old.rowid, old.pm_id, old.body);
    INSERT INTO fts_bodies(rowid, pm_id, body) VALUES (new.rowid, new.pm_id, new.body);
END;
```

### labels

```sql
CREATE TABLE labels (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    type    INTEGER NOT NULL,  -- 1=label, 2=folder, 3=system
    color   TEXT,
    display_order INTEGER
);
```

### sync_state

```sql
CREATE TABLE sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Keys:
--   last_event_id       — resume point for event loop
--   initial_sync_done   — '1' once all message metadata fetched
--   labels_synced_at    — Unix timestamp of last label fetch
```

## ProtonMail API Layer

### Authentication

Use `proton-python-client` (official, from ProtonMail/proton-python-client on GitHub).
It handles SRP-6a, session tokens, and refresh transparently.

```python
from proton.session import Session

session = Session()
session.authenticate(username, password)   # SRP handled internally
# Session persists access/refresh tokens
# save/load to disk between restarts
```

Session credentials stored in the system keychain (or encrypted config file).
Bridge already holds an authenticated session — we authenticate separately and
maintain our own token. This is a one-time interactive login; thereafter tokens
refresh silently.

### Event Loop

```python
# On first start
resp = await api.get("/mail/v4/events/latest")
last_event_id = resp["EventID"]
store("last_event_id", last_event_id)

# Ongoing — poll every 30s
while True:
    resp = await api.get(f"/mail/v4/events/{last_event_id}")

    if resp["Refresh"] & 1:
        await full_resync_messages()   # Server says: start over
    if resp["Refresh"] & 2:
        await full_resync_labels()

    for event in resp.get("Messages", []):
        await handle_message_event(event)

    for event in resp.get("Labels", []):
        await handle_label_event(event)

    last_event_id = resp["EventID"]
    store("last_event_id", last_event_id)

    if resp["More"]:
        continue   # More events waiting — fetch immediately
    await sleep(30)
```

### Message Event Handling

```python
async def handle_message_event(event: dict) -> None:
    action = event["Action"]
    pm_id  = event["ID"]

    if action == 0:  # Delete
        db.execute("DELETE FROM messages WHERE pm_id = ?", [pm_id])
        # Cascades to message_bodies, FTS index via triggers

    elif action == 1:  # Create
        msg = event["Message"]
        db.execute("INSERT INTO messages (...) VALUES (...)", [map_fields(msg)])
        body_indexer.enqueue(pm_id)   # Fetch body via IMAP asynchronously

    elif action in (2, 3):  # Update or UpdateFlags
        msg = event["Message"]
        db.execute("""
            UPDATE messages SET
                unread   = ?,
                label_ids = ?,
                folder   = ?,
                updated_at = ?
            WHERE pm_id = ?
        """, [msg["Unread"], json.dumps(msg["LabelIDs"]),
              derive_folder(msg["LabelIDs"]), now(), pm_id])
        # Moves are just label changes — no IMAP involved
```

### Mutations

```python
# Archive (apply Archive label, removes Inbox label automatically)
await api.put("/mail/v4/messages/label", {
    "LabelID": LABEL_ARCHIVE,  # "6"
    "IDs": [pm_id_1, pm_id_2, ...]
})

# Delete (move to Trash)
await api.put("/mail/v4/messages/label", {
    "LabelID": LABEL_TRASH,  # "3"
    "IDs": [pm_id_1, ...]
})

# Mark read
await api.put("/mail/v4/messages/read", {"IDs": [pm_id_1, ...]})
```

One API call. Atomic server-side. The event loop will confirm the change
— but we also update SQLite optimistically immediately so reads are
consistent without waiting for the next poll.

## Body Indexer

Bodies are fetched once per message when it's created. Bridge decrypts
transparently — we fetch via IMAP as plaintext.

```python
class BodyIndexer:
    """Background worker: fetch decrypted bodies via IMAP, index in FTS5."""

    async def run(self) -> None:
        while True:
            pm_id = await self._queue.get()
            message_id = db.get("SELECT message_id FROM messages WHERE pm_id = ?", pm_id)
            if not message_id:
                continue
            try:
                body = await self._fetch_body(message_id)
                db.execute("INSERT OR REPLACE INTO message_bodies (pm_id, body) VALUES (?, ?)",
                           [pm_id, body])
                db.execute("UPDATE messages SET body_indexed = 1 WHERE pm_id = ?", [pm_id])
            except Exception as e:
                logger.warning("body_fetch_failed", pm_id=pm_id, error=str(e))
                # Re-enqueue with backoff — not critical, search degrades gracefully

    async def _fetch_body(self, message_id: str) -> str:
        """IMAP SEARCH by Message-ID → FETCH body → return plaintext."""
        uid = await imap.search_by_message_id(message_id)  # existing code
        raw = await imap.fetch_body(uid)
        return extract_plaintext(raw)   # html2text if HTML, else raw
```

Concurrency: 3 workers (configurable). Backpressure: queue bound of 1000.
Body indexing is best-effort — search degrades gracefully (metadata-only
results) for un-indexed messages, with a note in the response.

### Initial Sync Body Fetch

For the initial import (e.g. 80K messages), IMAP per-message search would
be too slow. Instead, do a bulk folder-level FETCH:

```python
# Per folder: FETCH all UIDs with Message-ID header + body in one command
await imap.select(folder)
uids = await imap.search("ALL")
# Fetch in chunks of 200
for chunk in chunks(uids, 200):
    responses = await imap.fetch(chunk, ["BODY[HEADER.FIELDS (MESSAGE-ID)]", "BODY[TEXT]"])
    for uid, data in responses:
        mid = parse_message_id(data["BODY[HEADER.FIELDS (MESSAGE-ID)]"])
        body = data["BODY[TEXT]"]
        pm_id = db.get("SELECT pm_id FROM messages WHERE message_id = ?", mid)
        if pm_id:
            index_body(pm_id, body)
```

One IMAP command per chunk of 200 messages. A folder of 65K messages = 325
IMAP commands. At ~100ms each: ~30s. Acceptable for a one-time startup.

## Search

Replace notmuch + luqum with SQLite FTS5 + structured query builder.

```python
async def search(query: str, limit: int = 20) -> list[Message]:
    # Parse Gmail-style query into SQL components
    sql_query = translate_to_sql(query)
    # e.g. "from:bob subject:invoice is:unread" →
    #   WHERE sender_email LIKE '%bob%'
    #     AND subject LIKE '%invoice%'
    #     AND unread = 1

    # Full-text part (if free text in query)
    if sql_query.fts_terms:
        rows = db.execute("""
            SELECT m.* FROM messages m
            JOIN fts_bodies f ON f.pm_id = m.pm_id
            WHERE fts_bodies MATCH ?
              AND {sql_query.where}
            ORDER BY rank, m.date DESC
            LIMIT ?
        """, [sql_query.fts_terms, *sql_query.params, limit])
    else:
        rows = db.execute(f"""
            SELECT * FROM messages
            WHERE {sql_query.where}
            ORDER BY date DESC
            LIMIT ?
        """, [*sql_query.params, limit])
```

Gmail-style operators map to:

| Operator | SQL |
|----------|-----|
| `from:bob` | `sender_email LIKE '%bob%'` |
| `subject:invoice` | `subject LIKE '%invoice%'` |
| `is:unread` | `unread = 1` |
| `in:inbox` | `folder = 'INBOX'` |
| `has:attachment` | `has_attachments = 1` |
| `older_than:30d` | `date < unixepoch() - 2592000` |
| `newer_than:7d` | `date > unixepoch() - 604800` |
| free text | `fts_bodies MATCH ?` |

No more luqum dependency. No more notmuch query translation.

## Startup Sequence

```
1. Load config, open SQLite
2. Authenticate to ProtonMail API (load saved session or prompt)
3. If initial_sync_done = '0':
   a. Fetch all labels → populate labels table
   b. Fetch all message metadata (paginated) → populate messages table
   c. Start body indexer (background, bulk IMAP fetch per folder)
   d. Set initial_sync_done = '1'
4. Start event loop (background task, poll every 30s)
5. Start body indexer worker (if not already running)
6. Start MCP server — accept connections immediately
```

Step 3 may take minutes for a large mailbox. The server is usable immediately
after step 6 — metadata search works from the start. Full-text search becomes
available as bodies are indexed in the background (step 3c).

## Module Map

### New Modules

| Module | Purpose |
|--------|---------|
| `proton_api.py` | ProtonMail HTTP API client (auth, requests, rate limiting) |
| `event_loop.py` | Event polling, dispatch to handlers |
| `db.py` | SQLite connection, migrations, query helpers |
| `body_indexer.py` | IMAP body fetch queue, FTS indexing |
| `query_builder.py` | Gmail-style query → SQL translator (replaces luqum) |

### Modified Modules

| Module | Changes |
|--------|---------|
| `imap.py` | Remove mutations (COPY/DELETE/EXPUNGE). Keep SEARCH + FETCH for body indexer. |
| `tools/batch.py` | Replace IMAP batch ops with ProtonMail API bulk label calls |
| `tools/managing.py` | Mutations via ProtonMail API, not IMAP |
| `tools/searching.py` | Query via SQLite FTS5, not notmuch |
| `tools/listing.py` | Read from SQLite messages table, not Maildir |
| `tools/reading.py` | Read body from message_bodies, not Maildir files |
| `server.py` | Lifespan: API auth, event loop, body indexer |

### Removed

| Module | Replacement |
|--------|-------------|
| `store.py` | SQLite messages table |
| `search.py` (notmuch) | `query_builder.py` + SQLite FTS5 |
| `sync.py` (mbsync) | `event_loop.py` |
| `idle.py` | Event loop (covers all folders, not just INBOX) |

### Unchanged

| Module | Why |
|--------|-----|
| `sender.py` | SMTP sending works fine |
| `composer.py` | Message construction unchanged |
| `convert.py` | HTML → markdown unchanged |
| `models.py` | Will evolve but core shapes stay |
| `config.py` | New settings added, existing mostly removed |

## Configuration Changes

```
# Removed (no longer needed)
EMAIL_MCP_IMAP_HOST / PORT / USERNAME / PASSWORD  →  still needed for body fetch
EMAIL_MCP_MBSYNC_BIN / CHANNEL                   →  removed
EMAIL_MCP_NOTMUCH_BIN                             →  removed
EMAIL_MCP_INBOX_SYNC_INTERVAL                     →  removed
EMAIL_MCP_NIGHTLY_SYNC_ENABLED / HOUR             →  removed
EMAIL_MCP_IDLE_ENABLED                            →  removed
EMAIL_MCP_REINDEX_DEBOUNCE                        →  removed

# New
EMAIL_MCP_PM_USERNAME                   # ProtonMail login
EMAIL_MCP_PM_PASSWORD                   # ProtonMail password (or keychain)
EMAIL_MCP_DB_PATH                       # SQLite file path (default: ~/.local/share/email-mcp/db.sqlite)
EMAIL_MCP_EVENT_POLL_INTERVAL           # Seconds between event polls (default: 30)
EMAIL_MCP_BODY_INDEXER_WORKERS          # Concurrent IMAP body fetches (default: 3)
EMAIL_MCP_BODY_INDEXER_QUEUE_SIZE       # Max queued fetches (default: 1000)

# Kept (Bridge IMAP still used for body fetch)
EMAIL_MCP_IMAP_HOST / PORT / USERNAME / PASSWORD
```

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| `proton-python-client` is auth-only | Write event loop + API calls ourselves; use `go-proton-api` Go source as type reference |
| ProtonMail API undocumented officially | `go-proton-api` (official Go library) + `secure-mail-documentation-project` OpenAPI YAML |
| Event loop `Refresh=1` (full resync) | Implement full resync path; rare in practice |
| Rate limiting on event poll | Respect 30s minimum; exponential backoff on 429 |
| Initial body indexing slow (65K msgs) | Bulk IMAP FETCH per folder; background; FTS degrades gracefully |
| API auth session expiry | `proton-python-client` handles refresh; persist session to disk |
| Bridge IMAP still needed | Body fetch only — acceptable dependency; Bridge is already running |

## Implementation Phases

### Phase 1: Foundation (no user-visible change)
- `db.py` — SQLite schema, migrations
- `proton_api.py` — auth, raw HTTP calls, session persistence
- `event_loop.py` — poll, parse events, update SQLite (metadata only)
- Unit tests for all three

### Phase 2: Initial Sync
- Fetch all labels, all message metadata on first start
- `body_indexer.py` — IMAP body fetch queue, FTS indexing
- Bulk folder-level IMAP FETCH for initial body import
- Unit + integration tests

### Phase 3: Switch Read Path
- `tools/listing.py` — read from SQLite
- `tools/reading.py` — read body from message_bodies
- `tools/searching.py` — SQLite FTS5 + query_builder
- `query_builder.py` — Gmail-style → SQL
- Run both v3 and v4 read paths in parallel, validate results match

### Phase 4: Switch Mutation Path
- `tools/managing.py` + `tools/batch.py` — ProtonMail label API
- Remove IMAP COPY+DELETE
- Remove mbsync, notmuch dependencies

### Phase 5: Cleanup
- Remove `store.py`, `sync.py`, `idle.py`, `search.py` (notmuch)
- Trim `imap.py` to body-fetch-only
- Remove mbsync/notmuch from system deps
- Update `CLAUDE.md`, architecture docs

## What This Fixes

- **UIDVALIDITY chaos** → gone. We don't use IMAP UIDs for identity.
- **mbsync breaking on restart** → gone. No mbsync.
- **Stale notmuch index** → gone. SQLite updated by event loop in real time.
- **"not found in folder X"** → gone. Moves are label changes; no folder search.
- **Trash→Trash COPY rejected** → gone. Delete is a label call.
- **Zero-progress batch loops** → gone. Event loop catches deletions immediately.
- **65K-message Archive scan** → gone. Events, not scans.
- **notmuch 22s reindex** → gone. SQLite updates are sub-millisecond.
