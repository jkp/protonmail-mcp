# email-mcp: Architecture v2

## Overview

A self-contained MCP server that manages a local email mirror with full-text
search. Point it at any IMAP + SMTP server and it provides AI agents with
searchable, operable email via the Model Context Protocol.

The server owns the full lifecycle: sync, indexing, reading, searching,
composing, and sending. No external mail client required.

## Design Principles

1. **Maildir is the source of truth.** All read operations go through local
   files. No IMAP round-trips for reading, searching, or listing.
2. **Sync is managed, not assumed.** The server owns mbsync and notmuch as
   subprocesses, triggering them at the right times.
3. **Always responsive.** MCP tools respond immediately with whatever data is
   available. Sync status is surfaced, never blocks requests.
4. **IMAP-provider agnostic.** Works with any standard IMAP server. Protonmail
   Bridge is one deployment, not the identity.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│                      MCP Clients                        │
│              (Claude, other AI agents)                   │
└────────────────────────┬────────────────────────────────┘
                         │ MCP (stdio / SSE+HTTP)
┌────────────────────────▼────────────────────────────────┐
│                    email-mcp server                      │
│                                                          │
│  ┌──────────┐  ┌───────────┐  ┌──────────┐  ┌────────┐ │
│  │ MCP Tools│  │ Sync Mgr  │  │ Search   │  │Composer│ │
│  │          │  │           │  │ Engine   │  │        │ │
│  │ list     │  │ mbsync    │  │          │  │ reply  │ │
│  │ search   │  │ notmuch   │  │ luqum    │  │ fwd    │ │
│  │ read     │  │ IDLE/poll │  │ notmuch2 │  │ send   │ │
│  │ compose  │  │           │  │          │  │        │ │
│  │ manage   │  │           │  │          │  │        │ │
│  └────┬─────┘  └─────┬─────┘  └────┬─────┘  └───┬────┘ │
│       │              │             │             │      │
│  ┌────▼──────────────▼─────────────▼─────────────▼────┐ │
│  │                   Maildir Store                     │ │
│  │          stdlib: mailbox + email + pathlib          │ │
│  └────────────────────────┬───────────────────────────┘ │
│                           │                              │
└───────────────────────────┼──────────────────────────────┘
                            │ filesystem
              ┌─────────────▼─────────────┐
              │    ~/Mail/<account>/       │
              │    ├── INBOX/cur/          │
              │    ├── Archive/cur/        │
              │    ├── Sent/cur/           │
              │    ├── .notmuch/           │
              │    └── ...                 │
              └───────────────────────────┘
                            ▲
              mbsync (bidirectional sync)
                            │
              ┌─────────────▼─────────────┐
              │   IMAP Server (any)        │
              │   e.g. Protonmail Bridge   │
              │   localhost:1143           │
              └───────────────────────────┘
```

## Components

### 1. Sync Manager

Owns `mbsync` and `notmuch` as managed subprocesses. Responsible for keeping
the local Maildir in sync with the remote IMAP server and the full-text index
up to date.

**Sync triggers:**
- **IMAP IDLE notification** — new mail arrived on server (preferred)
- **Timer-based polling** — fallback, every N seconds (configurable, default 60s)
- **Post-mutation hook** — after any local write (move, delete, flag change),
  sync immediately to push changes upstream
- **Manual** — MCP tool `sync_now` for on-demand refresh

**Sync cycle:**
```
trigger → mbsync -a → notmuch new → update sync_status
```

Each step is an async subprocess call. The sync manager serialises sync cycles
(no concurrent mbsync runs) using an asyncio Lock.

**IMAP IDLE implementation:**

`aioimaplib` has critical bugs with IDLE (EXISTS stuck in buffer, hangs on
connection loss). Two viable alternatives:

- **Option A: `IMAPClient` in a thread.** Mature, battle-tested IDLE. Run in
  `asyncio.to_thread()`, signal the event loop when new mail arrives. Simpler,
  more reliable.
- **Option B: Short-interval polling.** Run `mbsync` every 30-60s via
  `asyncio.create_task` with `asyncio.sleep`. No IDLE connection to manage.
  Good enough for most use cases.

Recommend starting with **Option B** (polling) and adding IDLE later if
latency matters. Polling is simpler, has no connection management edge cases,
and mbsync is fast when there's nothing new.

**State:**
```python
@dataclass
class SyncStatus:
    state: Literal["initializing", "syncing", "ready", "error"]
    last_sync: datetime | None
    last_index: datetime | None
    message_count: int
    error: str | None
```

### 2. Maildir Store

Thin wrapper around stdlib `mailbox.Maildir` + `email` + `pathlib`. All local
email operations go through this layer.

**Reading:**
```python
# Parse a message from its Maildir path
path = Path("~/Mail/account/INBOX/cur/1234.hostname,U=42:2,S")
with path.open("rb") as f:
    msg = email.parser.BytesParser(policy=email.policy.default).parse(f)

body = msg.get_body(preferencelist=("html", "plain"))
attachments = list(msg.iter_attachments())
```

**Moving (archive, trash, etc.):**
```python
# Move file from INBOX to Archive on disk
src = maildir_root / "INBOX" / "cur" / filename
dst = maildir_root / "Archive" / "cur" / filename
src.rename(dst)
# Trigger sync to push change to IMAP
await sync_manager.sync()
```

**Flag management:**
Maildir flags are encoded in the filename suffix after `:2,`:
- `S` = Seen (read)
- `F` = Flagged (starred)
- `R` = Replied
- `D` = Draft
- `T` = Trashed

Rename the file to change flags. notmuch's `tags.to_maildir_flags()` can also
do this.

**Key operations:**
| Operation | Implementation |
|-----------|---------------|
| List folders | `os.listdir(maildir_root)`, filter directories |
| List emails | notmuch query or `mailbox.Maildir` iteration |
| Read email | `email.parser.BytesParser` on file path |
| Move email | `pathlib.Path.rename()` + sync |
| Delete email | Move to Trash folder + sync |
| Archive email | Move to Archive folder + sync |
| Flag/unflag | Rename file to change flag suffix + sync |
| List attachments | `email.message.EmailMessage.iter_attachments()` |
| Download attachment | Read attachment content from parsed message |

### 3. Search Engine

Full-text search via notmuch, with Gmail-style query translation via luqum
(carried forward from v1).

**Two options for notmuch integration:**

#### Option A: notmuch2 CFFI bindings (recommended)
```python
import notmuch2

db = notmuch2.Database(path=maildir_root, mode="ro")
for msg in db.messages("from:alice AND tag:unread"):
    yield SearchResult(
        message_id=msg.messageid,
        path=msg.path,
        subject=msg.header("Subject"),
        authors=msg.header("From"),
        date=msg.date,
        tags=set(msg.tags),
        folder=extract_folder(msg.path),
    )
```

Advantages:
- No subprocess overhead (direct C FFI calls)
- No JSON serialization round-trip
- `db.find(message_id)` for O(1) lookup
- Access to Message-ID, tags, all headers
- Database handle reuse across queries

Requirement: `libnotmuch` shared library must be installed.

Synchronous API — wrap in `asyncio.to_thread()` for async context.

#### Option B: notmuch CLI (fallback)
Keep current subprocess approach. Simpler deployment (no libnotmuch dependency
at the Python level), but slower and requires JSON parsing.

**Query translation** (luqum — carried forward from v1):

Gmail-style queries are parsed into an AST and rewritten to notmuch syntax:
```
has:attachment  →  tag:attachment
is:unread       →  tag:unread
in:archive      →  folder:Archive
label:important →  tag:important
newer_than:7d   →  date:7days..
```

**No UID resolution needed.** Search results include the Maildir file path.
Reading uses that path directly. No IMAP UID translation, no himalaya
envelope lookup, no fallback to stale UIDs. The entire UID mismatch problem
disappears.

### 4. Composer

Handles reply, forward, and new message composition. Replaces himalaya's
template workflow with direct email construction using stdlib.

**Reply:**
```python
def build_reply(original: EmailMessage, body: str, reply_all: bool = False) -> EmailMessage:
    reply = EmailMessage()
    reply["Subject"] = f"Re: {strip_re(original['Subject'])}"
    reply["To"] = original["Reply-To"] or original["From"]
    reply["In-Reply-To"] = original["Message-ID"]
    reply["References"] = build_references(original)
    if reply_all:
        reply["Cc"] = merge_recipients(original)
    reply.set_content(quote_body(original, body))
    return reply
```

**Forward:**
```python
def build_forward(original: EmailMessage, to: str, body: str) -> EmailMessage:
    fwd = EmailMessage()
    fwd["Subject"] = f"Fwd: {strip_fwd(original['Subject'])}"
    fwd["To"] = to
    fwd["References"] = build_references(original)
    fwd.set_content(format_forwarded(original, body))
    # Re-attach original attachments
    for att in original.iter_attachments():
        fwd.add_attachment(att.get_content(), maintype=..., subtype=..., filename=...)
    return fwd
```

**Sending:**
```python
async def send(message: EmailMessage) -> None:
    await aiosmtplib.send(
        message,
        hostname=config.smtp_host,
        port=config.smtp_port,
        start_tls=config.smtp_starttls,
        username=config.smtp_username,
        password=config.smtp_password,
    )
    # Save to Sent folder
    save_to_maildir(maildir_root / "Sent", message)
    await sync_manager.sync()
```

### 5. MCP Tools

Same tool interface as v1, but backed by Maildir operations instead of
himalaya subprocess calls.

| Tool | Description | Backing |
|------|-------------|---------|
| `list_folders` | List available mail folders | `os.listdir` on Maildir |
| `list_emails` | List emails in a folder | notmuch query |
| `read_email` | Read full email content | `email.parser` on Maildir file |
| `search` | Full-text search | notmuch + luqum translation |
| `send` | Send new email | `aiosmtplib` |
| `reply` | Reply to an email | Composer + `aiosmtplib` |
| `forward` | Forward an email | Composer + `aiosmtplib` |
| `archive` | Move to Archive | File move + sync |
| `move_email` | Move between folders | File move + sync |
| `delete` | Move to Trash | File move + sync |
| `list_attachments` | List email attachments | `email` MIME parsing |
| `download_attachment` | Get attachment content | `email` MIME parsing |
| `sync_now` | Trigger immediate sync | Sync manager |
| `sync_status` | Check sync state | Sync manager status |

**Email identification:** Tools identify emails by **notmuch Message-ID**
(globally unique, never changes) or by **file path** — not IMAP UIDs.

Search results return Message-IDs. `read_email` accepts a Message-ID and
looks up the file via `notmuch2.Database.find(message_id)`.

### 6. Configuration

Pydantic-settings, environment variables, `.env` file support.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EMAIL_MCP_", env_file=".env")

    # IMAP (for sync)
    imap_host: str = "127.0.0.1"
    imap_port: int = 1143
    imap_username: str
    imap_password: str
    imap_starttls: bool = True

    # SMTP (for sending)
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_username: str
    smtp_password: str
    smtp_starttls: bool = False

    # Maildir
    maildir_root: Path = Path("~/.local/share/email-mcp/mail").expanduser()

    # Sync
    sync_interval_seconds: int = 60
    sync_on_startup: bool = True
    mbsync_bin: str = "mbsync"

    # Search
    notmuch_bin: str = "notmuch"
    use_notmuch_bindings: bool = True  # use notmuch2 CFFI if available

    # Server
    transport: Literal["stdio", "http"] = "stdio"
    host: str = "0.0.0.0"
    port: int = 8025
    log_level: str = "INFO"

    # Auth (optional, for HTTP transport)
    github_client_id: str | None = None
    github_client_secret: str | None = None
    oauth_base_url: str | None = None
    oauth_allowed_users: str | None = None
```

### 7. Generated Configuration

The server generates and manages `mbsync` and `notmuch` configuration files
automatically from its own settings. Users never write `.mbsyncrc` or
`.notmuch-config` manually.

**mbsync config** (generated at `{maildir_root}/.mbsyncrc`):
```
IMAPAccount email-mcp
Host 127.0.0.1
Port 1143
User jamie@kirkpatrick.email
Pass <password>
SSLType STARTTLS
CertificateFile /path/to/bridge-cert.pem

IMAPStore remote
Account email-mcp

MaildirStore local
Path ~/Mail/email-mcp/
Inbox ~/Mail/email-mcp/INBOX
SubFolders Verbatim

Channel email-mcp
Far :remote:
Near :local:
Patterns *
Create Both
Remove Both
Expunge Both
SyncState *
```

**notmuch config** (generated at `{maildir_root}/.notmuch/config`):
```ini
[database]
path=/home/user/Mail/email-mcp

[new]
tags=unread;inbox
ignore=.mbsyncstate;.strstrm

[search]
exclude_tags=deleted;spam

[maildir]
synchronize_flags=true
```

## Lifecycle

### First Run (Cold Start)

```
1. Server starts
2. Check maildir_root exists
   ├─ No  → Create directory structure
   │        Generate mbsync config
   │        Generate notmuch config
   │        Run initial mbsync (full sync — may take minutes)
   │        Run notmuch new (initial index)
   │        Set state = "ready"
   └─ Yes → Check .notmuch exists
            ├─ No  → Run notmuch new
            └─ Yes → Set state = "ready"
3. Start sync loop (timer/IDLE)
4. Start MCP server (accept connections)
```

**MCP tools are available immediately at step 4**, even during initial sync.
Tools that depend on local data return empty results with a status indicator
during initialization:

```json
{
  "results": [],
  "sync_status": "initializing",
  "message": "Initial sync in progress. Results will be available shortly."
}
```

### Steady State

```
MCP request ──→ Read from Maildir/notmuch ──→ Respond immediately
                     (no network required)

Sync timer fires ──→ mbsync -a ──→ notmuch new ──→ New mail searchable

Mutating tool call ──→ Local Maildir change ──→ mbsync -a ──→ Change on server
```

### Error Recovery

| Scenario | Behaviour |
|----------|-----------|
| mbsync fails | Log error, retry next cycle, surface in `sync_status` |
| notmuch new fails | Log error, search uses stale index, surface in status |
| IMAP server unreachable | Reads still work (local), writes queued or errored |
| notmuch2 not installed | Fall back to CLI subprocess |
| Maildir corrupted | Log error, suggest re-sync via `sync_now --full` |

## Dependency Stack

### Python packages
| Package | Purpose | Notes |
|---------|---------|-------|
| `fastmcp` | MCP server framework | Carried from v1 |
| `pydantic-settings` | Configuration | Carried from v1 |
| `structlog` | Structured logging | Carried from v1 |
| `html2text` | HTML → Markdown | Carried from v1 |
| `luqum` | Query parsing/translation | Carried from v1 |
| `aiosmtplib` | Async SMTP sending | New |
| `notmuch2` | Search bindings (optional) | New, optional |

### System dependencies
| Tool | Purpose | Required |
|------|---------|----------|
| `mbsync` | IMAP ↔ Maildir sync | Yes |
| `notmuch` | Full-text search + indexing | Yes |
| `libnotmuch` | CFFI bindings support | Optional (falls back to CLI) |

### Removed from v1
| Dependency | Reason |
|------------|--------|
| `himalaya` | Replaced by Maildir operations + aiosmtplib |

## Module Layout

```
src/email_mcp/
├── __init__.py
├── __main__.py          # Entry point
├── server.py            # FastMCP instance, tool imports, startup
├── config.py            # Pydantic settings
├── sync.py              # Sync manager (mbsync + notmuch new)
├── store.py             # Maildir operations (read, move, delete, flags)
├── search.py            # notmuch search + luqum query translation
├── composer.py          # Reply/forward/new message construction
├── sender.py            # aiosmtplib wrapper
├── convert.py           # HTML → Markdown (carried from v1)
├── models.py            # Pydantic models (Email, Folder, Attachment, etc.)
├── auth.py              # OAuth middleware (carried from v1)
└── tools/
    ├── __init__.py
    ├── listing.py       # list_folders, list_emails
    ├── reading.py       # read_email, list_attachments, download_attachment
    ├── searching.py     # search
    ├── composing.py     # send, reply, forward
    └── managing.py      # archive, move_email, delete, sync_now, sync_status
```

## Migration from v1

### What carries forward
- FastMCP server setup + auth middleware
- Pydantic-settings config pattern
- luqum query translation (the whole `_GmailToNotmuch` transformer)
- HTML → Markdown conversion (`html2text`)
- MCP tool interface (same tool names and signatures)
- Test patterns (pytest-asyncio, `Client(mcp)` in-memory transport)
- Live integration test harness

### What gets replaced
- `himalaya.py` → `store.py` + `composer.py` + `sender.py`
- `notmuch.py` → `search.py` (using notmuch2 bindings)
- All tools internals (same interface, different backing)
- `models.py` (shaped around `email.message.EmailMessage`, not himalaya JSON)
- `template.py` (eliminated — no more `<#part>` parsing)

### Build or rebuild?

**Rebuild.** The core abstraction changes from "himalaya subprocess wrapper"
to "Maildir-native email engine". The MCP tool interface is the stable outer
shell; everything behind it changes. Carrying forward individual modules
(config, auth, convert, luqum) is copy-paste, not incremental refactoring.

The v1 codebase and its tests remain as reference. The new project starts
clean with the proven patterns copied in.

## Implementation Order

### Phase 1: Foundation
1. Project scaffold (`email-mcp`, pyproject.toml, config)
2. `store.py` — Maildir read, move, delete, flag operations
3. `search.py` — notmuch2 bindings + luqum query translation
4. `models.py` — Pydantic models for Email, Folder, Attachment, SearchResult

### Phase 2: Read Path
5. Tools: `list_folders`, `list_emails`, `read_email`
6. Tools: `search`
7. Tools: `list_attachments`, `download_attachment`
8. HTML → Markdown conversion integration

### Phase 3: Write Path
9. `composer.py` — reply/forward construction
10. `sender.py` — aiosmtplib wrapper
11. Tools: `send`, `reply`, `forward`
12. Tools: `archive`, `move_email`, `delete`

### Phase 4: Sync
13. `sync.py` — mbsync + notmuch new subprocess management
14. Config generation (mbsyncrc, notmuch config)
15. Startup lifecycle (cold start, warm start)
16. Timer-based sync loop
17. Tools: `sync_now`, `sync_status`

### Phase 5: Polish
18. IMAP IDLE (optional, via IMAPClient in thread)
19. Auth middleware (carry from v1)
20. Live integration tests
21. Documentation
