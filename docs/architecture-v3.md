# email-mcp: Architecture v3

## Overview

A Maildir-native MCP server for email with full-text search, IMAP-authoritative
mutations, and optimistic local state.

## Design Principles

1. **Reads are local.** All read operations use Maildir files + notmuch index.
   No network round-trips for listing, reading, or searching.
2. **Mutations are IMAP-first.** Move, archive, delete, and flag changes go
   directly to IMAP. The server is the source of truth for mutations.
3. **Local state is optimistic.** After a successful IMAP mutation, the local
   Maildir is updated immediately to keep reads consistent without waiting for
   a full sync.
4. **Sync is tiered.** Different folders sync at different frequencies based on
   their size and importance. INBOX is near-real-time. Archive is nightly.
5. **Errors are explicit.** If data might be stale or a file can't be found
   after a recent mutation, the tool says so rather than returning bad data.

## What Changed from v2

v2 treated Maildir as the source of truth for both reads AND writes. Mutations
were local file moves, then mbsync pushed changes to IMAP. This caused:

- UID collisions when moving files between folders
- APPEND rejections (bad headers re-uploaded to strict IMAP servers)
- 16-minute Archive syncs blocking interactive use
- Race conditions between concurrent mbsync runs
- Stale state after mbsync lock conflicts

v3 splits the model: reads stay local, mutations go to IMAP. mbsync becomes a
background consistency tool, not a real-time sync mechanism.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                      MCP Clients                             │
│              (Claude, other AI agents)                        │
└────────────────────────┬────────────────────────────────────┘
                         │ MCP (stdio / SSE+HTTP)
┌────────────────────────▼────────────────────────────────────┐
│                    email-mcp server                          │
│                                                              │
│  ┌──────────┐  ┌───────────┐  ┌──────────┐  ┌────────────┐ │
│  │ MCP Tools│  │  IMAP     │  │ Search   │  │  Composer   │ │
│  │          │  │  Mutator  │  │ Engine   │  │  + Sender   │ │
│  │ list     │  │           │  │          │  │             │ │
│  │ search   │  │ COPY/STORE│  │ notmuch  │  │ aiosmtplib  │ │
│  │ read     │  │ DELETE    │  │ luqum    │  │             │ │
│  │ compose  │  │ FLAGS     │  │          │  │             │ │
│  │ manage   │  │           │  │          │  │             │ │
│  └────┬─────┘  └─────┬─────┘  └────┬─────┘  └──────┬─────┘ │
│       │              │             │                │       │
│  ┌────▼──────────────▼─────────────▼────────────────▼─────┐ │
│  │                  Maildir Store (read)                    │ │
│  │          stdlib: email.parser + pathlib                  │ │
│  └────────────────────────┬───────────────────────────────┘ │
│                           │                                  │
│  ┌────────────────────────▼───────────────────────────────┐ │
│  │                   Sync Engine                           │ │
│  │                                                         │ │
│  │  INBOX: IDLE + 60s periodic mbsync (0.3s)              │ │
│  │  Archive: IDLE + nightly mbsync (16 min)               │ │
│  │  Other: nightly mbsync                                  │ │
│  │  notmuch: debounced reindex after mutations (22s)      │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                              │
└──────────────────────────────────────────────────────────────┘
                            │ filesystem          │ IMAP
              ┌─────────────▼──────────┐  ┌──────▼───────────┐
              │  ~/Mail/<account>/      │  │  IMAP Server     │
              │  ├── INBOX/cur/         │  │  (ProtonMail     │
              │  ├── Archive/cur/       │  │   Bridge or any  │
              │  ├── Sent/cur/          │  │   IMAP server)   │
              │  ├── .notmuch/          │  │                  │
              │  └── ...                │  │  localhost:1143   │
              └────────────────────────┘  └──────────────────┘
```

## Mutation Flow

All state-changing operations (move, archive, delete, flag changes) follow
the same pattern:

```
1. Tool receives request (e.g. archive message X)
2. IMAP command executes (COPY + STORE \Deleted + EXPUNGE)
   └─ If IMAP fails → return error, no local changes
3. Optimistic local move (rename file between Maildir folders)
4. Signal reindex (debounced notmuch new)
5. Return success immediately
```

The IMAP command is authoritative. The local move is a cache update. If
step 3 fails (file already gone, race condition), it's harmless — the
nightly sync will fix it.

### IMAP Mutator

New module: `imap.py`. Async IMAP client for mutations only (not sync).

```python
class ImapMutator:
    """Execute mutations directly on the IMAP server."""

    async def connect(self) -> None:
        """Establish IMAP connection with STARTTLS."""

    async def move(self, message_id: str, to_folder: str) -> None:
        """Move a message by Message-ID.

        1. SEARCH HEADER Message-ID <id>  → get UID
        2. COPY <uid> <to_folder>
        3. STORE <uid> +FLAGS (\Deleted)
        4. EXPUNGE
        """

    async def delete(self, message_id: str) -> None:
        """Move to Trash via IMAP."""

    async def set_flags(self, message_id: str, flags: str) -> None:
        """Set flags (e.g. \Seen, \Flagged) via STORE."""

    async def archive(self, message_id: str) -> None:
        """Move to Archive."""

    async def archive_thread(self, message_id: str) -> None:
        """Find all thread messages and archive them."""
```

Uses `aioimaplib` or `IMAPClient` (in asyncio.to_thread). The connection
is persistent, reconnecting on failure.

### Optimistic Local Move

After IMAP confirms the mutation, the file is moved locally:

```python
async def _optimistic_move(message_id: str, to_folder: str) -> None:
    """Move the local Maildir file to match the IMAP mutation."""
    path = _find_by_message_id(message_id)  # notmuch or scan
    if path is None:
        return  # File not local yet — nightly sync will catch it
    dest_dir = maildir_root / to_folder / ("cur" if ":2," in path.name else "new")
    dest_name = re.sub(r",U=\d+", "", path.name)  # Strip IMAP UID
    path.rename(dest_dir / dest_name)
```

UID stripping is still needed because the file gets a new UID in the
destination folder on the IMAP side. mbsync will assign the correct UID
on the next sync of that folder.

### Reindex Strategy

`notmuch new` takes ~22 seconds to scan 80K files. Running it after every
mutation would be wasteful. Instead:

- **Debounced timer**: After any mutation, start a 60-second timer. If
  more mutations happen within that window, reset the timer. When the
  timer fires, run `notmuch new`.
- **At most 1 running, at most 1 pending**: Same singleton pattern as
  sync. If notmuch is already running and the timer fires, set a dirty
  flag.
- **Immediate fallback for reads**: If a read fails because notmuch
  points to a stale path, scan Maildir folders directly for the
  Message-ID before giving up.

## Sync Engine

### Two-Tier Sync

There are only two tiers: INBOX (fast, real-time) and everything else
(nightly batch).

| Folder | Messages | Sync time | Strategy |
|--------|----------|-----------|----------|
| INBOX | ~168 | 0.3s | IDLE + periodic mbsync |
| Trash | ~854 | 13 min | Nightly only |
| Sent | ~10K | ~5+ min | Nightly only |
| Archive | ~65K | 16 min | Nightly only |

ProtonMail Bridge is the bottleneck — anything over a few hundred
messages takes minutes, not seconds. There's no useful middle ground
between "every 60s" and "nightly".

### INBOX Sync

INBOX is small (~168 messages) and syncs in 0.3 seconds. Two mechanisms
keep it fresh:

1. **IMAP IDLE**: Persistent connection via aioimaplib. When the server
   signals a change (new message, expunge, flag change), trigger an
   immediate `mbsync protonmail:INBOX` + `notmuch new`.

2. **Periodic poll**: Every N seconds (configurable, default 60s), run
   `mbsync protonmail:INBOX` as a safety net in case IDLE misses an
   event (connection drop, Bridge bug, etc.).

IDLE only makes sense on INBOX — that's where mail arrives. Other
folders only change via mutations (which we handle via IMAP) or
external clients (caught by the nightly sync). No IDLE on Archive,
Sent, or Trash.

### Nightly Full Sync

All other folders sync once per night at a configurable hour (default
03:00). Runs `mbsync protonmail` (all folders) + `notmuch new`.

This is the ground truth reset: fixes any drift from optimistic moves,
external changes made via web/phone, or anything else that accumulated
during the day.

- **On-demand**: `sync_now` tool available for manual full sync trigger.

### Sync Singleton

At most **1 sync running, at most 1 pending**:

```python
class SyncEngine:
    _running: bool = False
    _dirty: bool = False
    _lock: asyncio.Lock

    async def request_sync(self, folders: list[str] | None = None) -> None:
        """Request a sync. Coalesces multiple requests."""
        if self._running:
            self._dirty = True
            return
        async with self._lock:
            self._running = True
            try:
                await self._do_sync(folders)
                while self._dirty:
                    self._dirty = False
                    await self._do_sync(folders)
            finally:
                self._running = False
```

### Per-Folder mbsync

mbsync supports syncing individual folders via channel patterns:

```bash
mbsync protonmail:INBOX     # 0.3s — just INBOX
mbsync protonmail:Sent       # ~30s — just Sent
mbsync protonmail:Archive    # ~16 min — just Archive
mbsync protonmail            # all folders — nightly only
```

This uses the existing mbsync config with `Patterns *` — the `:FOLDER`
suffix selects which pattern to sync.

## Startup Sequence

```
1. Server starts
2. Connect IMAP (for mutations + IDLE)
3. mbsync protonmail:INBOX (0.3s) — authoritative inbox immediately
4. notmuch new (22s, background — non-blocking)
5. Start IDLE listener (INBOX only)
6. Start periodic INBOX sync (every 60s)
7. Schedule nightly full sync
8. Start MCP server (accept connections)
```

The server is usable after step 3 (~1 second). Search becomes available
after step 4 (~22 seconds). Archive, Sent, Trash etc. rely on the
nightly sync for ground truth — during the day, optimistic local moves
keep them consistent enough for reads.

### Configuration

```
EMAIL_MCP_SYNC_ON_STARTUP=true          # Run INBOX sync on startup
EMAIL_MCP_INBOX_SYNC_INTERVAL=60        # Seconds between INBOX syncs
EMAIL_MCP_NIGHTLY_SYNC_HOUR=3           # Hour (0-23) for nightly full sync
EMAIL_MCP_NIGHTLY_SYNC_ENABLED=true     # Enable/disable nightly sync
EMAIL_MCP_IDLE_ENABLED=true             # Enable IMAP IDLE on INBOX
EMAIL_MCP_REINDEX_DEBOUNCE=60           # Seconds to debounce notmuch new
```

## Read Path

Unchanged from v2. All reads go through local Maildir + notmuch.

### Stale Path Resilience

When notmuch returns a file path that no longer exists (because of an
optimistic move or a sync lag), the read path falls back:

```python
async def read_email(message_id: str) -> dict:
    # 1. Try notmuch path (fast, usually correct)
    path = notmuch_find(message_id)
    if path and path.exists():
        return parse_email(path)

    # 2. Scan Maildir folders for the Message-ID (slower, resilient)
    path = scan_maildir_for_message_id(message_id)
    if path:
        return parse_email(path)

    # 3. Not found locally — explain why
    return {
        "error": "not_found_locally",
        "message_id": message_id,
        "detail": "This email may have been recently moved. "
                  "It will be available after the next sync cycle.",
    }
```

The scan in step 2 is cheap for a single Message-ID — grep the first
few KB of each file in `cur/` and `new/` across a handful of folders.
Only triggered on cache miss, not on every read.

## Error Handling

### Explicit Staleness

Tools never silently return wrong data. If the local state might be
inconsistent:

```json
{
  "results": [...],
  "stale_warning": "Archive was last synced 18 hours ago. Some results may be missing.",
  "last_sync": {
    "INBOX": "2026-03-11T17:00:00Z",
    "Archive": "2026-03-11T03:00:00Z"
  }
}
```

### IMAP Mutation Failures

If the IMAP command fails, no local changes are made:

```json
{
  "error": "imap_error",
  "detail": "IMAP COPY failed: connection refused",
  "action": "No changes were made. Try again or check IMAP connectivity."
}
```

### Read Failures

If a file is missing after an optimistic move:

```json
{
  "error": "not_found_locally",
  "message_id": "<abc@example.com>",
  "detail": "This email was recently moved and hasn't been reindexed yet. Try again in ~60 seconds."
}
```

## Module Changes

### New Modules

| Module | Purpose |
|--------|---------|
| `imap.py` | IMAP mutator — COPY, STORE, DELETE, EXPUNGE, SEARCH by Message-ID |
| `idle.py` | IMAP IDLE listener — persistent connection for INBOX only |

### Modified Modules

| Module | Changes |
|--------|---------|
| `sync.py` | Tiered sync engine: per-folder mbsync, singleton pattern, nightly scheduler, debounced notmuch |
| `store.py` | Optimistic local moves (post-IMAP), stale path resilience on reads |
| `tools/managing.py` | Mutations call ImapMutator first, then optimistic local move |
| `config.py` | New settings: idle, nightly sync, reindex debounce, per-folder intervals |
| `server.py` | Startup sequence: INBOX sync → IDLE → periodic timers |

### Unchanged Modules

| Module | Why |
|--------|-----|
| `search.py` | notmuch queries unchanged |
| `composer.py` | Message construction unchanged |
| `sender.py` | SMTP sending unchanged |
| `convert.py` | HTML→markdown unchanged |
| `models.py` | Data models unchanged |
| `tools/listing.py` | Read-only, unchanged |
| `tools/reading.py` | Read-only (plus stale path fallback) |
| `tools/searching.py` | Read-only, unchanged |
| `tools/composing.py` | Send via SMTP, unchanged |

## Implementation Order

### Phase 1: IMAP Mutator
1. `imap.py` — connect, SEARCH by Message-ID, COPY, STORE, EXPUNGE
2. Update `tools/managing.py` — mutations via IMAP, then optimistic local move
3. Tests: mock IMAP, verify local move after IMAP success, verify no
   local move on IMAP failure

### Phase 2: Tiered Sync
4. `sync.py` — per-folder mbsync, singleton pattern, debounced notmuch
5. Startup sequence: INBOX sync, UIDNEXT check, background reindex
6. Periodic INBOX sync (every 60s)
7. Nightly full sync scheduler

### Phase 3: IMAP IDLE
8. `idle.py` — persistent IDLE connection for INBOX
9. IDLE → trigger INBOX sync on change
10. IDLE reconnection on connection loss (re-issue every 29 min per RFC)
11. Integration with sync engine

### Phase 4: Resilience
12. Stale path fallback in read_email
13. Staleness warnings in list/search results
14. Explicit error messages for all failure modes
15. Live integration tests

## Measured Performance (ProtonMail Bridge, 80K messages)

| Operation | Time | Notes |
|-----------|------|-------|
| mbsync INBOX | 0.3s | 168 messages, delta sync |
| mbsync Archive | 16 min | 65K messages, delta sync |
| mbsync all folders | ~20 min | 80K messages total |
| notmuch new (no changes) | 22s | Full filesystem scan |
| IMAP SEARCH by Message-ID | <1s | Single command |
| IMAP COPY + EXPUNGE | <1s | Single message move |
| Local file rename | <1ms | Optimistic move |

## IMAP IDLE Notes

ProtonMail Bridge supports IDLE (confirmed via CAPABILITY response:
`AUTH=PLAIN ID IDLE IMAP4REV1 STARTTLS`). Known considerations:

- Bridge has had bug fixes around IDLE (expunge sequence numbers,
  timeout handling) — implementation should be defensive
- IDLE only monitors one folder per connection — we only need INBOX
- RFC recommends re-issuing IDLE every 29 minutes
- Connection drops may be silent — periodic INBOX sync is the safety net
- aioimaplib supports IDLE natively with `idle_start()` / `wait_server_push()`
- Alternative: `IMAPClient` in `asyncio.to_thread()` — more mature IDLE

Recommendation: start with periodic INBOX sync (Phase 2) and add IDLE
(Phase 3) as an enhancement. The system works without IDLE — it just
has slightly higher latency (~60s worst case) for new mail detection.

No IDLE on Archive/Sent/Trash — these folders only change via our own
IMAP mutations (handled optimistically) or external clients (caught by
the nightly sync). IDLE would only tell us "something changed" with no
affordable way to act on it (sync takes minutes).
