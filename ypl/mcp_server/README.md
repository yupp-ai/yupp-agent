# Yupp MCP Server

Internal development tooling that allows engineers' AI agents (Claude Code, Cursor, etc.) to access Yupp production infrastructure for debugging and analysis.

> ⚠️ **Dev tokens (`yupp_dev_*`) are deprecated.** OAuth is now the recommended auth path for all MCP clients. Dev tokens continue to work during the deprecation window but will be removed entirely after **2026-06-15**. See [Legacy: Dev Tokens (deprecated)](#legacy-dev-tokens-deprecated) for migration guidance, or watch for the `X-Auth-Deprecation` response header on any dev-token request.

## Use the service

The recommended path for all clients (Claude Code, Cursor, Claude Cowork, web UIs) is the OAuth-secured remote MCP endpoint. You sign in once with your `@example.com` Google account; the client manages the token from there.

### Claude Code (terminal)

```bash
# Add the OAuth-secured remote MCP server (no token to copy/paste)
claude mcp add --transport http agcouch-mcp-server --scope user \
  https://agcouch-mcp-oauth.example.com/mcp
```

When you next launch Claude Code in the yupp-agent repo, run `/mcp`. You'll be prompted to authenticate with Google — sign in with your `@example.com` account and the connection is permanent for that workstation.

### Claude Code on the web (Cloud Agent)

1. Go to https://claude.ai/code
2. Click the ☁️ environment button (bottom-right of the chat box)
3. Click the settings icon (⚙️)
4. Add `https://agcouch-mcp-oauth.example.com/mcp` as a Custom MCP Server (OAuth)
5. Set Network Access to **Full** so Cloud Agent can reach the MCP endpoint

You will not need an environment variable. The OAuth flow runs on first connect.

### Claude Cowork

1. Settings → Connectors
2. **Add Custom Connector**
3. Name: `Agcouch MCP`
4. Remote MCP server URL: `https://agcouch-mcp-oauth.example.com/mcp`
5. When the **Connect** button activates, click it and complete the Google OAuth flow.
6. (Optional staging:) repeat with `https://agcouch-mcp-oauth-staging.example.com/mcp`.

### Cursor / other MCP clients

Any MCP client that supports OAuth-secured streamable-HTTP transports will work. Point it at `https://agcouch-mcp-oauth.example.com/mcp` and complete the Google flow on first connect.

To set up the staging MCP server to debug staging issues, please refer to [Staging Server Setup](#staging-server-setup).

## Architecture

```
Engineer's MCP Client (Claude Code / Cursor / Cowork / Web)
         ↓
    HTTPS — OAuth (Google) → JWT bearer
         ↓
    ┌───────────────────────────────────────────────────┐
    │           Cloud Run (yupp-agent:mcp mode)         │
    │                                                   │
    │     MCP_SERVER_MODE=OAUTH (recommended)           │
    │     GoogleProvider (FastMCP)                      │
    │                                                   │
    │     [Legacy] MCP_SERVER_MODE=DEV_TOKEN            │
    │     DevTokenAuthMiddleware → response carries     │
    │       X-Auth-Deprecation header                   │
    │                                                   │
    │              FastMCP /mcp endpoint                │
    │              (validates auth, logs audit)         │
    └───────────────────────────────────────────────────┘
                          ↓
         Tools execute with service account credentials
                          ↓
         GCP Logging / Cloud SQL / Vercel Logs
```

## Available Tools

### 1. `search_gcp_logs`
Search Google Cloud Logging for Yupp MIND production logs.

**Use cases:**
- Debug production errors
- Trace specific requests
- Investigate user issues

**Parameters:**
- `query` (required): GCP logging filter syntax
  - Examples: `severity=ERROR`, `labels.user_id="123"`, `textPayload=~"timeout"`
- `hours_back` (optional): Hours to search back (default: 24)
- `max_results` (optional): Max results to return (default: 100)

**Example:**
The MCP client invokes `search_gcp_logs` directly via the MCP protocol over the OAuth-secured connection — no manual HTTP / Bearer token plumbing required.

### 2. `search_vercel_logs`
Search Vercel deployment logs (via GCP logging).

**Use cases:**
- Debug frontend deployment issues
- Investigate Vercel-specific errors
- Trace frontend requests

**Parameters:**
- `query` (optional): Additional filter (combined with Vercel filter)
- `hours_back` (optional): Hours to search back (default: 24)
- `max_results` (optional): Max results (default: 100)

**Implementation:**
Automatically adds `resource.labels.service_name="vercel-log-drain"` to the query.

### 3. `query_yuppdb`
Execute read-only SQL queries on Yupp production database.

**Use cases:**
- Investigate user data issues
- Check database state
- Analyze patterns

**Parameters:**
- `sql` (required): SELECT query only
- `max_rows` (optional): Max rows to return (default: 1000)

**Security:**
- Only SELECT queries allowed
- Blocks INSERT, UPDATE, DELETE, DROP, etc.
- Executes against read replica (if configured)
- Results truncated to prevent excessive data retrieval

**Example:**
```sql
SELECT id, email, created_at
FROM users
WHERE email = 'user@example.com'
LIMIT 10
```

## Authentication

The MCP server supports two authentication modes, controlled by the `MCP_SERVER_MODE` environment variable. **OAuth is the recommended mode.** DevToken mode remains available during the deprecation window but is no longer the default for new deployments.

### OAUTH mode (recommended)

Uses Google OAuth via FastMCP's `GoogleProvider` for browser-based authentication:

- Users authenticate via the standard Google OAuth flow
- Email-domain validation ensures only allowed domains (e.g. `example.com`) can access
- Session tokens are stored encrypted in Redis
- No manual token issuance, no token revocation drills, no shell-profile editing
- Per-engineer identity is verified end-to-end — every `mcp_audit_logs` row is tied to a verified Google identity

This mode is ideal for:
- Web-based MCP clients (Claude Cowork, Cloud Agent)
- Interactive desktop clients (Claude Code, Cursor, etc.)
- Self-service access for authorized engineers

### DEV_TOKEN mode (deprecated, kept for backwards compatibility)

Uses Bearer tokens issued via the admin Streamlit page or the `manage create-mcp-token` CLI command:

```
Authorization: Bearer yupp_dev_<token>
```

Tokens are:
- **Scoped to engineer email** — each token belongs to a specific Yupp engineer
- **Hashed with bcrypt** — never stored in plaintext
- **Optionally expiring** — can set expiration date
- **Revocable** — can be revoked at any time with reason

**Deprecation status (phase 5a, started 2026-05-08):**

- Every dev-token request now carries an `X-Auth-Deprecation` response header.
- The admin Streamlit page (`/admin_mcp_tokens`) shows a deprecation banner; please do not issue new dev tokens.
- Dev tokens remain accepted at least until **2026-06-15**. Phase 5b (PR-B) deletes the middleware, the `mcp_dev_token` table, and all related CLI / Slack tooling. Coordinate with the platform team if a use case absolutely requires a dev token after the cutover.

See [Legacy: Dev Tokens (deprecated)](#legacy-dev-tokens-deprecated) for the historical setup steps and migration guidance.

### Mode configuration

Set the mode via environment variable:

```bash
# OAuth mode (recommended)
MCP_SERVER_MODE=OAUTH

# DevToken mode (deprecated; kept for migration period)
MCP_SERVER_MODE=DEV_TOKEN
```

For OAuth mode, additional configuration is required:
- `MCP_OAUTH_GOOGLE_CLIENT_ID` — Google OAuth client ID
- `MCP_OAUTH_GOOGLE_CLIENT_SECRET` — Google OAuth client secret
- `MCP_OAUTH_JWT_SIGNING_KEY` — JWT signing key for token management
- `MCP_OAUTH_STORAGE_ENCRYPTION_KEY` — Fernet key for Redis token encryption

## Audit Logging

Every tool invocation is logged to `mcp_audit_logs` table with:
- **Who**: Engineer email and token type (`OAUTH` — or `DEV_TOKEN` during the deprecation window)
- **What**: Tool name and parameters
- **Result**: Success/failure, error message, result summary
- **When**: Timestamp
- **Performance**: Execution time in milliseconds
- **Context**: IP address, user agent, OAuth callback URL (if applicable)

Audit logging works identically for both authentication modes, ensuring complete traceability regardless of how users authenticate.

## Agent Configuration

### Project Configuration (`.mcp.json`)

The repository includes a project-level `.mcp.json` that points the agcouch MCP server at the OAuth endpoint by default. After phase 5b ships, this is the only path that will keep working:

```json
{
  "mcpServers": {
    "agcouch-mcp-server": {
      "type": "http",
      "url": "https://agcouch-mcp-oauth.example.com/mcp"
    }
  }
}
```

The MCP client manages OAuth tokens itself; no shell environment variables required.

### Staging Server Setup

A staging MCP server is available for debugging staging-specific issues. Connect any OAuth-capable MCP client to:

```
https://agcouch-mcp-oauth-staging.example.com/mcp
```

For Claude Code:

```bash
claude mcp add --transport http agcouch-mcp-server-staging --scope user \
  https://agcouch-mcp-oauth-staging.example.com/mcp
```

Sign in with your `@example.com` Google account on first connect.

### MCP Protocol Details

**Endpoint:**
- `POST /mcp/` — Streamable HTTP transport for bidirectional JSON-RPC messages

**Required Headers (OAuth):**
- `Authorization: Bearer <oauth_jwt>` — managed by the MCP client itself
- `Content-Type: application/json`
- `Accept: application/json`

The server uses Streamable HTTP transport (modern replacement for SSE):
- Single endpoint instead of separate SSE + messages endpoints
- Better scalability with stateless design
- Full bidirectional communication via HTTP streaming

### REST API (for testing)

For simpler integrations or testing, REST convenience endpoints are also available. Use them with a short-lived OAuth JWT obtained out-of-band:

```bash
# List available tools
curl https://agcouch-mcp-oauth.example.com/mcp/tools \
  -H "Authorization: Bearer <oauth_jwt>"
```

**Note:** The REST endpoints are a convenience layer. Use the `/mcp` endpoint for full MCP protocol compliance.

### Documentation References

- [Claude Code MCP Configuration](https://docs.anthropic.com/en/docs/claude-code/mcp)
- [Claude Code Settings](https://docs.anthropic.com/en/docs/claude-code/settings)
- [Claude Code Headless/Agent Mode](https://docs.anthropic.com/en/docs/claude-code/headless)

## Deployment

### Cloud Run Configuration

```bash
# Build and deploy in OAuth mode
gcloud run deploy agcouch-mcp-oauth \
  --source . \
  --region us-central1 \
  --set-env-vars="BACKEND_OPERATING_MODE=mcp,MCP_SERVER_MODE=OAUTH" \
  --service-account yupp-mcp-server@yupp-llms.iam.gserviceaccount.com
```

### Environment Variables

Required:
- `BACKEND_OPERATING_MODE=mcp` — Enables MCP server mode
- `GCP_PROJECT_ID=yupp-llms` — Google Cloud project
- `POSTGRES_HOST`, `POSTGRES_PASSWORD`, etc. — Database credentials

Authentication Mode:
- `MCP_SERVER_MODE=OAUTH` — Use Google OAuth authentication (**recommended**)
- `MCP_SERVER_MODE=DEV_TOKEN` — Use DevToken authentication (deprecated; will be removed after 2026-06-15)

OAuth Mode (required when `MCP_SERVER_MODE=OAUTH`):
- `MCP_OAUTH_GOOGLE_CLIENT_ID` — Google OAuth client ID
- `MCP_OAUTH_GOOGLE_CLIENT_SECRET` — Google OAuth client secret
- `MCP_OAUTH_JWT_SIGNING_KEY` — JWT signing key (generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
- `MCP_OAUTH_STORAGE_ENCRYPTION_KEY` — Fernet encryption key (generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
- `REDIS_URL` — Redis URL for OAuth token storage

Optional:
- `ALLOWED_MCP_EMAIL_DOMAINS=example.com` — Restrict access to specific email domains (applies to both modes)

## Security Considerations

### Identity & Tokens
- OAuth: identity verified end-to-end via Google; tokens are short-lived, rotated by the MCP client
- DevToken (legacy): tokens hashed with bcrypt, scoped to engineer email, revocable, optional expiration
- Per-engineer accountability — every audit-log row is attributable to a verified identity

### Database Security
- Only SELECT queries allowed
- Dangerous keywords (INSERT, UPDATE, DELETE, DROP, etc.) blocked
- Results limited to prevent data exfiltration
- Consider using read replica for queries

### Audit Trail
- Every tool call logged with full context
- Logs retained for compliance
- Can track who accessed what and when
- Execution times logged for performance monitoring

### Network Security
- Requires HTTPS
- Bearer token authentication
- Service runs on Google Cloud with IAM permissions
- Can restrict by IP if needed

## Future Enhancements

Potential additions:
- [ ] Rate limiting per engineer (e.g., 100 calls/hour)
- [ ] BigQuery export of audit logs
- [ ] Slack notifications for sensitive queries
- [ ] Web dashboard for token management
- [ ] Tool-level permissions (some engineers can't query DB)
- [ ] Multi-region deployment
- [ ] WebSocket support for real-time MCP protocol
- [ ] Additional tools (deploy, rollback, etc.)

## Troubleshooting

### "Invalid token" / "Token expired"
- (OAuth) Re-run the OAuth flow in your client (`/mcp` in Claude Code re-auths automatically)
- (Dev token, deprecated) Confirm the token has not been revoked from the admin page; if it has, please migrate to OAuth instead of issuing a new dev token

### `X-Auth-Deprecation` header in responses
- That's expected on every dev-token response. Migrate the affected client to OAuth before 2026-06-15.

### "Only SELECT queries are allowed"
- Database queries must start with SELECT
- No write operations permitted
- Use migrations or direct SQL for schema changes

### "No results found"
- Check GCP logging filter syntax
- Verify time range (default is last 24 hours)
- Ensure logs exist for the query period

## Development

### Starting the Server Locally

```bash
# Using entrypoint.sh (production-like)
cd /path/to/yupp-agent
./ypl/mcp_server/entrypoint.sh

# Or using uvicorn directly
uvicorn ypl.mcp_server.server:app --host 0.0.0.0 --port 8080

# With auto-reload for development
uvicorn ypl.mcp_server.server:app --host 0.0.0.0 --port 8080 --reload
```

### Architecture

The server follows the repo conventions:

```
ypl/mcp_server/
├── __init__.py
├── server.py          # Starlette app with routes, mode-based middleware, lifespan
├── core.py            # FastMCP instance creation (mode-aware) and audit logging
├── auth_oauth.py      # OAuth authentication (GoogleProvider with domain validation)
├── auth_dev_token.py  # [Deprecated] DevToken authentication (create, validate, revoke, middleware)
├── mcp_tools.py       # Tool implementations (GCP logs, DB queries)
├── tasks.py           # TaskIQ tasks for async token management
├── entrypoint.sh      # Production startup script
└── README.md
```

The MCP protocol is implemented using:
- **FastMCP** — High-level MCP server framework (tools registered via decorators)
- **Streamable HTTP transport** — Modern replacement for SSE (single `/mcp` endpoint)
- **Starlette** — Lightweight ASGI framework for routing and middleware
- **Mode-based authentication**:
  - OAUTH mode: FastMCP's GoogleProvider handles OAuth flow (recommended)
  - DEV_TOKEN mode: `DevTokenAuthMiddleware` validates `yupp_dev_*` tokens (deprecated)

```
MCP_SERVER_MODE selection:

OAUTH mode (recommended)            DEV_TOKEN mode (deprecated)
      │                                  │
      ▼                                  ▼
FastMCP GoogleProvider          DevTokenAuthMiddleware
   (FastMCP internal)              (Starlette)
      │                                  │
      └──────────────┬───────────────────┘
                     ▼
          ToolCallLoggingMiddleware
            (logs to mcp_audit_logs)
```

All database functions manage their own sessions internally with `@retry_db` decorator for resilience against intermittent DB issues.

### Testing Tools Locally

```python
import asyncio

# Test GCP logs search
from ypl.mcp_server.mcp_tools import search_gcp_logs

async def test_search():
    result = await search_gcp_logs(
        query="severity=INFO",
        hours_back=1,
        max_results=5
    )
    print(result)

asyncio.run(test_search())
```

```python
import asyncio

# Test database query (uses read replica, manages its own session)
from ypl.mcp_server.mcp_tools import query_yuppdb

async def test_query():
    result = await query_yuppdb(
        sql="SELECT id, email FROM users LIMIT 5",
        max_rows=10
    )
    print(result)

asyncio.run(test_query())
```

## Database Schema

### `mcp_audit_logs` table
```sql
-- Enum types
CREATE TYPE mcpauditlogstatus AS ENUM ('SUCCESS', 'FAILED');
CREATE TYPE mcptokentype AS ENUM ('DEV_TOKEN', 'OAUTH');

CREATE TABLE mcp_audit_logs (
    mcp_audit_log_id UUID PRIMARY KEY,
    -- Who (supports both DevToken and OAuth)
    mcp_dev_token_id UUID REFERENCES mcp_dev_tokens(mcp_dev_token_id),  -- NULL for OAuth (and removed entirely after phase 5b)
    email VARCHAR(255),              -- User email (from token or OAuth claims)
    callback_url TEXT,               -- OAuth callback origin (NULL for DevToken)
    token_type mcptokentype NOT NULL DEFAULT 'OAUTH',
    -- What
    tool_name VARCHAR(255) NOT NULL,
    tool_parameters JSONB NOT NULL,
    -- Result
    status mcpauditlogstatus NOT NULL,
    error_message TEXT,
    result_summary TEXT,
    -- Context
    execution_time_ms INTEGER,
    ip_address VARCHAR(45),
    user_agent TEXT,
    session_id VARCHAR(255),
    -- Timestamps
    created_at TIMESTAMP NOT NULL,
    modified_at TIMESTAMP NOT NULL,
    deleted_at TIMESTAMP
);
```

## Support

For issues or questions:
- (OAuth) Re-run the OAuth flow in your client; check `mcp_audit_logs` for failed entries
- (Dev token, deprecated) Review token status in the admin Streamlit page (`/admin_mcp_tokens`) — but please migrate to OAuth rather than issue/replace dev tokens
- Contact platform team for access issues

---

## Legacy: Dev Tokens (deprecated)

> ⚠️ **This section is preserved for users who have not yet migrated.** The `yupp_dev_*` token machinery — middleware, admin page, CLI commands, `mcp_dev_token` DB table — will be removed in phase 5b after **2026-06-15**. Please migrate to OAuth following the [Use the service](#use-the-service) section above. Every dev-token response now carries an `X-Auth-Deprecation` response header for self-service detection.

### Why migrate

- Dev tokens require manual issuance, manual rotation, and manual revocation drills.
- Every dev token sits in shell profiles and `.env` files — broad surface area for accidental leakage.
- OAuth ties every audit-log entry to a verified Google identity, no shared-secret middleman.
- Phase 5b (after the deprecation window closes) deletes the middleware and the DB table — pre-existing tokens stop working.

### How to migrate

1. Pick the OAuth setup section above that matches your client (Claude Code / Cloud Agent / Cowork / Cursor).
2. After confirming OAuth works, remove `AGCOUCH_MCP_TOKEN` (and `AGCOUCH_MCP_TOKEN_STAGING`) from your shell profile / `.env` files.
3. Drop the `headers.Authorization` line from any project-local `.mcp.json` you maintain — the OAuth client manages its own token.

### Legacy setup (pre-OAuth)

These instructions are kept for reference only and **should not be used for new setups**.

#### Issue a dev token

Token issuance previously ran through the `/create-mcp-token` Slack command in `#agentic-couch` or the admin Streamlit page (`/admin_mcp_tokens`). The Streamlit page now shows a deprecation banner. Please do **not** request new dev tokens — switch to OAuth instead.

#### Configure a dev token (legacy)

```bash
export AGCOUCH_MCP_TOKEN="yupp_dev_YOUR_TOKEN_HERE"
```

Or, in `.mcp.json`:

```json
{
  "mcpServers": {
    "agcouch-mcp-server": {
      "type": "http",
      "url": "https://agcouch-mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${AGCOUCH_MCP_TOKEN}"
      }
    }
  }
}
```

#### CLI commands (legacy)

The token-management CLI is still present during the deprecation window:

```bash
# Issue
python -m ypl.cli mcp-create-token \
  --email engineer@example.com \
  --description "Token for debugging prod issues" \
  --expires-days 90

# List
python -m ypl.cli mcp-list-tokens
python -m ypl.cli mcp-list-tokens --email engineer@example.com
python -m ypl.cli mcp-list-tokens --active-only

# Revoke
python -m ypl.cli mcp-revoke-token <token-id> \
  --revoked-by <email@example.com> \
  --reason "Migrated to OAuth"

# Audit
python -m ypl.cli mcp-audit-log
python -m ypl.cli mcp-audit-log --email engineer@example.com
python -m ypl.cli mcp-audit-log --tool search_gcp_logs

# Stats
python -m ypl.cli mcp-stats
python -m ypl.cli mcp-stats --days 30
```

These commands and the underlying `mcp_dev_token` DB table will be deleted in phase 5b.

### `mcp_dev_tokens` table (to be dropped in phase 5b)

```sql
-- Enum type for token status
CREATE TYPE mcptokenstatus AS ENUM ('ACTIVE', 'REVOKED', 'EXPIRED');

CREATE TABLE mcp_dev_tokens (
    mcp_dev_token_id UUID PRIMARY KEY,
    token_lookup_key VARCHAR(8) NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    email VARCHAR(255) NOT NULL,
    description TEXT,
    created_at TIMESTAMP NOT NULL,
    modified_at TIMESTAMP NOT NULL,
    deleted_at TIMESTAMP,
    last_used_at TIMESTAMP,
    expires_at TIMESTAMP,
    status mcptokenstatus NOT NULL DEFAULT 'ACTIVE',
    revoked_at TIMESTAMP,
    revoked_by VARCHAR(255),
    revoked_reason TEXT
);
```
