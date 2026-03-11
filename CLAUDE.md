# email-mcp

Maildir-native MCP server for email with full-text search.

## Quick Start

```bash
uv sync                    # install deps
uv run pytest              # run tests
uv run email-mcp           # start server (stdio)
```

## Architecture

- **Maildir** is the source of truth — all read operations use local files
- **mbsync** syncs IMAP ↔ Maildir bidirectionally
- **notmuch** provides full-text search + indexing
- **aiosmtplib** sends email via SMTP
- **Message-ID** identifies emails (not IMAP UIDs)
- **FastMCP** server with 14 tools, stdio or HTTP transport

## Key Modules

| Module | Purpose |
|--------|---------|
| `server.py` | FastMCP instance, tool imports, entry point |
| `config.py` | Pydantic settings from env vars (EMAIL_MCP_ prefix) |
| `store.py` | Maildir ops: read, list, move, delete, flags |
| `search.py` | notmuch search + luqum Gmail query translation |
| `composer.py` | Reply/forward/new message construction (stdlib email) |
| `sender.py` | aiosmtplib SMTP wrapper |
| `sync.py` | mbsync + notmuch new subprocess management |
| `convert.py` | HTML → markdown via html2text |
| `models.py` | Pydantic models (Email, Folder, Attachment, SearchResult, SyncStatus) |
| `tools/` | Tool implementations (listing, reading, searching, composing, managing) |

## Testing

Tests use `pytest-asyncio` with `asyncio_mode = "auto"`.
Store tests run against temporary Maildir fixtures (real files, no mocks).
SMTP sending is mocked via `unittest.mock`.

```bash
uv run pytest -v            # verbose
uv run pytest --cov         # with coverage
```

## Configuration

All env vars use `EMAIL_MCP_` prefix:
- `EMAIL_MCP_IMAP_HOST`, `EMAIL_MCP_IMAP_PORT`, `EMAIL_MCP_IMAP_USERNAME`, `EMAIL_MCP_IMAP_PASSWORD`
- `EMAIL_MCP_SMTP_HOST`, `EMAIL_MCP_SMTP_PORT`, `EMAIL_MCP_SMTP_USERNAME`, `EMAIL_MCP_SMTP_PASSWORD`
- `EMAIL_MCP_MAILDIR_ROOT`, `EMAIL_MCP_FROM_NAME`, `EMAIL_MCP_FROM_ADDRESS`
- `EMAIL_MCP_TRANSPORT` (stdio/http), `EMAIL_MCP_HOST`, `EMAIL_MCP_PORT`
- `EMAIL_MCP_NOTMUCH_BIN`, `EMAIL_MCP_MBSYNC_BIN`, `EMAIL_MCP_SYNC_INTERVAL_SECONDS`

## Prerequisites (external)

- `mbsync` (isync) for IMAP ↔ Maildir sync
- `notmuch` for full-text search + indexing
- IMAP server (e.g. Protonmail Bridge on localhost)
