# Data Flow Invariants

This document describes the consistency model between the four layers of
email-mcp and the rules that must never be violated.

## The Four Layers

```
ProtonMail Server  (authoritative, remote)
        |
  ProtonMail Bridge  (local IMAP/SMTP proxy)
        |
     Maildir  (local files on disk)
        |
   notmuch index  (full-text search over Maildir)
```

Data flows **down** via mbsync (IMAP server -> Maildir) and notmuch new
(Maildir -> index). Mutations flow **up** via IMAP commands (tool -> Bridge
-> server).

## The Golden Rule

> **All mutations MUST go through IMAP.**

IMAP is the single source of truth. When you want to move, delete, flag, or
otherwise mutate a message:

1. Send the IMAP command (COPY, STORE, EXPUNGE) to Bridge
2. Bridge propagates to ProtonMail server
3. On next mbsync, Maildir reflects the change
4. On next notmuch new, the index reflects the change

The `optimistic_move` in `store.py` is an **acceleration**, not a mutation
path. It moves the local Maildir file immediately after IMAP success so
reads don't have to wait for the next sync cycle. But the IMAP command is
what makes the change real.

## Anti-patterns

### Never modify Maildir files directly to mutate state

Deleting, moving, or renaming files in the Maildir without going through
IMAP creates a split-brain:

- **mbsync gets confused** -- its sync state tracks UIDs and filenames. If
  you delete a file it thinks should exist, it may re-download it, skip it,
  or propagate the deletion upstream depending on configuration. The behavior
  is unpredictable.
- **notmuch index goes stale** -- it points to file paths that no longer
  exist, or has folder metadata derived from paths that are now wrong.
- **The IMAP server still has the message** -- the user's phone, webmail,
  and other clients will still show it.

This is exactly what happened when the test cleanup fixture (`_cleanup_test_files`)
deleted Maildir files directly. It caused cascading inconsistencies that
required a full rebuild to fix.

### Never trust notmuch folder info as authoritative

Notmuch derives folder information from Maildir file paths. This is usually
correct but can be stale or misleading:

- **After direct Maildir manipulation** (see above), paths are wrong.
- **Self-sent emails** have copies in multiple folders (Sent + INBOX).
  `SearchResult.folders` is a list for this reason -- a message can
  legitimately exist in more than one folder.
- **After IMAP mutations before reindex** (up to 60s), notmuch still shows
  the old folder.

Notmuch is a **search accelerator**, not a source of truth. When folder info
matters for correctness (e.g. which IMAP folder to SELECT), handle misses
gracefully -- the message may be in a different folder than notmuch thinks.

### Never bypass the sync cycle for reads

The sync cycle (mbsync + notmuch new) keeps the local state consistent. If
you read Maildir files that haven't been synced recently, you may see stale
data. The architecture handles this with tiered sync intervals:

- INBOX: IDLE + 60s polling (near real-time)
- All folders: nightly full sync
- On-demand: `sync_now` tool

## Consistency Windows

| Scenario | Staleness | Why |
|----------|-----------|-----|
| Read after IMAP mutation | ~0s | Optimistic local move |
| Search after IMAP mutation | 0-60s | Debounced notmuch reindex |
| Other client's changes visible | 0-60s | INBOX sync interval |
| Non-INBOX folder changes | Up to 24h | Nightly sync only |

## Test Cleanup

Test cleanup MUST use `search_and_delete` (IMAP path), not direct file
deletion. The `cleanup_test_emails` helper in `tests/live/conftest.py`
does this correctly -- it calls the MCP tool which goes through IMAP.

The session-scoped `_cleanup_test_files` fallback exists only as a
belt-and-suspenders for cases where the MCP server isn't available.
It should not be the primary cleanup mechanism.
