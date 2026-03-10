# ProtonMail MCP Server

MCP server for ProtonMail via Himalaya CLI and notmuch full-text search.

## Quick Start

```bash
uv sync                    # install deps
uv run pytest              # run tests
uv run protonmail-mcp      # start server (stdio)
```

## Architecture

- **himalaya CLI** (IMAP backend) → Protonmail Bridge (localhost:1143/1025)
- **notmuch CLI** → full-text search over mbsync Maildir
- **UID bridge**: notmuch → Maildir filenames (`,U=<uid>`) → himalaya IMAP UIDs
- **FastMCP** server with 12 tools, stdio or HTTP transport

## Key Modules

| Module | Purpose |
|--------|---------|
| `server.py` | FastMCP instance, tool imports, entry point |
| `config.py` | Pydantic settings from env vars |
| `himalaya.py` | Async subprocess wrapper for himalaya CLI |
| `notmuch.py` | Notmuch search + Maildir UID extraction |
| `models.py` | Pydantic models (Envelope, Message, Folder, SearchResult) |
| `convert.py` | HTML → markdown via html2text |
| `tools/` | Tool implementations (listing, reading, searching, composing, managing) |

## Testing

All subprocess calls are mocked. Tests use `pytest-asyncio` with `asyncio_mode = "auto"`.

Integration tests use FastMCP's in-memory `Client(mcp)` pattern.

```bash
uv run pytest -v            # verbose
uv run pytest --cov         # with coverage
```

## Configuration

See `.env.sample` for all env vars. Key ones:
- `HIMALAYA_BIN`, `HIMALAYA_CONFIG_PATH`, `HIMALAYA_ACCOUNT`
- `NOTMUCH_BIN`, `MAILDIR_ROOT`
- `PROTONMAIL_MCP_TRANSPORT` (stdio/http), `PROTONMAIL_MCP_HOST`, `PROTONMAIL_MCP_PORT`

## Prerequisites (external)

- `himalaya` configured with IMAP backend
- `mbsync` (isync) syncing to Maildir with native UID scheme (`,U=<uid>`)
- `notmuch` indexed against the mbsync Maildir
- Protonmail Bridge running on localhost
