# Deployment & Security Guide

## Architecture

email-mcp is a self-hosted MCP server that provides AI-assisted access to your ProtonMail inbox. It decrypts messages locally using your PGP keys, indexes them into SQLite (full-text + vector search), and exposes them via the MCP protocol over HTTP.

```
Claude.ai / MCP Client
        │
        ▼ (HTTPS via reverse proxy)
   ┌─────────────┐
   │  email-mcp   │  ← FastMCP stateless HTTP
   │  container   │  ← SQLite DB (bodies, FTS5, vectors)
   │              │  ← PGP key ring (in-memory, derived from session)
   └──────┬───────┘
          │ (HTTPS)
          ▼
   ProtonMail API
```

## Prerequisites

- Python 3.13+
- ProtonMail account with 2FA enabled
- A server to run the container (Linux recommended)
- Reverse proxy (NGINX, Caddy, or Tailscale Funnel) for TLS termination

## Initial Setup

### 1. Authenticate with ProtonMail

```bash
uv run email-mcp-auth
```

This prompts for your username, password, and 2FA code interactively. It produces a session file containing:
- API access/refresh tokens
- Derived mailbox passphrase (bcrypt hash of your password)
- Key salts for PGP key derivation

**Your password is not stored.** The passphrase is a one-way bcrypt derivative — it cannot be reversed to recover the login password.

### 2. Configure Environment

Create a `.env` file:

```bash
# Required
EMAIL_MCP_IMAP_USERNAME=your@protonmail.com

# Server
EMAIL_MCP_TRANSPORT=http
EMAIL_MCP_HOST=0.0.0.0
EMAIL_MCP_PORT=10143

# OAuth (optional, for authenticated access)
EMAIL_MCP_GITHUB_CLIENT_ID=your_github_oauth_app_id
EMAIL_MCP_GITHUB_CLIENT_SECRET=your_github_oauth_secret
EMAIL_MCP_OAUTH_BASE_URL=https://your-domain.com
EMAIL_MCP_OAUTH_ALLOWED_USERS=your_github_username
EMAIL_MCP_OAUTH_STATE_DIR=/data/oauth

# Embedding API (optional, for fast backfill)
EMAIL_MCP_TOGETHER_API_KEY=your_together_api_key

# Database
EMAIL_MCP_DB_PATH=/data/email.db
EMAIL_MCP_PROTON_SESSION_PATH=/data/proton_session.json
```

### 3. Run the Server

```bash
uv run email-mcp
```

On first start:
1. Loads PGP keys from ProtonMail API using cached passphrase
2. Syncs labels and message metadata (if not already done)
3. Background-indexes bodies (PGP decrypt via API) — INBOX first
4. Background-embeds bodies (Together API or local model) — INBOX first
5. Event loop polls for new messages every 30s

### 4. Seed Data (Optional)

To avoid re-downloading 98k messages on a new server, copy the SQLite database:

```bash
scp ~/.local/share/email-mcp/email.db server:/data/email.db
scp ~/.local/share/email-mcp/proton_session.json server:/data/proton_session.json
```

The server picks up from `last_event_id` — no duplicate work.

## Security Considerations

### Threat Model

This server holds decrypted email content. Understand what you're exposing:

| Asset | Risk if compromised |
|-------|-------------------|
| SQLite DB | Full plaintext email archive (bodies, metadata, attachments) |
| Session file | ProtonMail API access (read, write, delete, send) |
| Mailbox passphrase | Can decrypt PGP private key → decrypt any message |
| OAuth tokens | MCP server impersonation |

### What ProtonMail Encryption Protects

- **In transit**: TLS to ProtonMail API
- **At rest on ProtonMail servers**: PGP-encrypted, only you can decrypt
- **From ProtonMail themselves**: They never see your plaintext

### What It Does NOT Protect

Once decrypted for local indexing, protection is your server's security boundary. If an attacker gains access to the running container, they have everything.

### Mitigations

#### Session Revocability (Your Kill Switch)

If compromised, immediately revoke the session in ProtonMail:
**Settings → Security → Session Management → Revoke**

This instantly kills API access. The attacker keeps the local DB but loses the ability to read new mail, send as you, or delete messages.

#### Container Hardening

```dockerfile
# Minimal base image
FROM python:3.13-slim

# Read-only filesystem except data volume
# No shell if possible
# Drop all capabilities
# Run as non-root user

USER 1000:1000
VOLUME /data
```

Recommendations:
- **No shell access**: Use `--no-install-recommends`, remove bash if possible
- **Read-only root filesystem**: Mount `/data` as the only writable volume
- **Network policy**: Container should only reach `mail.proton.me:443` and `api.together.xyz:443` (for embeddings). Block all other egress.
- **No SSH, no extra ports**: Only expose the MCP HTTP port
- **Resource limits**: Set memory/CPU limits to prevent abuse

#### Reverse Proxy

**Do not expose the FastMCP server directly to the internet.**

Use a reverse proxy for:
- TLS termination (Let's Encrypt, Cloudflare, or Tailscale)
- Rate limiting
- Request size limits
- IP allowlisting (if applicable)

Example NGINX configuration:

```nginx
server {
    listen 443 ssl;
    server_name email-mcp.your-domain.com;

    ssl_certificate /etc/letsencrypt/live/your-domain/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain/privkey.pem;

    # Rate limiting
    limit_req_zone $binary_remote_addr zone=mcp:10m rate=30r/m;

    location /mcp {
        limit_req zone=mcp burst=10;
        proxy_pass http://127.0.0.1:10143;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;

        # Limit request body size
        client_max_body_size 1m;
    }
}
```

Alternatively, **Tailscale Funnel** provides zero-config HTTPS with automatic certificates and no open ports:

```bash
tailscale funnel --bg --https=443 http://127.0.0.1:10143
```

#### Cloudflare (Additional Layer)

If using Cloudflare in front of your reverse proxy:
- Enable WAF rules for common attack patterns
- Enable bot detection
- Set SSL mode to "Full (Strict)"
- Consider Access policies to restrict who can reach the endpoint

#### OAuth Authentication

GitHub OAuth restricts who can call the MCP tools. Configure `EMAIL_MCP_OAUTH_ALLOWED_USERS` to your GitHub username. Without a valid OAuth token, all API calls return 401.

The OAuth state is persisted to disk (`EMAIL_MCP_OAUTH_STATE_DIR`) so MCP clients don't need to re-authenticate after server restarts.

### What You Cannot Fully Prevent

If an attacker achieves remote code execution inside the container (e.g., via an exploit in FastMCP, uvicorn, or a dependency):

- They can read the SQLite database (decrypted email content)
- They can use the ProtonMail session tokens
- They can access the PGP passphrase from process memory

This is inherent to any email client running on a server. The mitigations above limit blast radius and provide a kill switch, but cannot prevent data access from within the process boundary.

**Defence in depth**: reverse proxy + OAuth + container isolation + network policy + monitoring + session revocability.

## Monitoring

The server logs structured JSON to `email-mcp.log`:

```bash
# Watch for auth failures
grep "auth.rejected" /data/email-mcp.log

# Watch for API errors
grep "error\|failed\|warning" /data/email-mcp.log

# Monitor embedding progress
grep "embed_progress" /data/email-mcp.log
```

Optional: configure ntfy push notifications for critical events:

```bash
EMAIL_MCP_NTFY_URL=https://ntfy.sh
EMAIL_MCP_NTFY_TOPIC=your-topic
```

## Backup & Recovery

### What to Back Up

- `email.db` — the full email index (metadata, bodies, FTS5, vectors)
- `proton_session.json` — API tokens + mailbox passphrase
- `oauth/` — OAuth client registrations

### Recovery

1. Deploy fresh container
2. Copy backed-up files to data volume
3. Start server — it resumes from `last_event_id`
4. If session expired: run `email-mcp-auth` to re-authenticate

### Session Rotation

ProtonMail sessions expire after 60 days of inactivity. If the server runs continuously, the refresh token keeps the session alive indefinitely. If the server is down for >60 days, re-authenticate with `email-mcp-auth`.
