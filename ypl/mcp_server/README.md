# Yupp MCP Server

Internal development tooling that allows engineers' AI agents (Claude Code, Cursor, etc.) to access Yupp production infrastructure for debugging and analysis.

## Use the service

Here's how to set it up to work with Claude Code:
- Type /create-mcp-token slack command in `#agentic-couch` slack channel, and dialog will show up.
- Upon submission, a token will be created and email to the example.com email you specified. (Create for other agcouchs are supported, but logged!)
- Copy your Access Token from the email you received, and add it to your bash environment.
  - e.g. `echo "export AGCOUCH_MCP_TOKEN=yupp_dev_{...}" >> ~/.zshrc` if you use zsh, or any other places where you keep environment variables. (Remember to `source ~/.zshrc` when you are done, so it's part of current terminal)
  - or `export AGCOUCH_MCP_TOKEN=yupp_dev_{...}` directly on your terminal if you don't want to it set for every terminal session.
- In your Claude Code, run `/mcp` command in yupp-mind repo, it should automatically discover the MCP server, and authenticate for you.
- Just ask Claude some question that requires knowledge of our DB, Bigquery, Redis, GCP (BE) and Vercel (FE) logs

For using Claude code on Web UI (remote agent). You should:
- go to https://claude.ai/code
- Click the ☁️ (environment) button (☁️ is located on the right bottom corner of the main chat box)
- Click the settings icon (⚙️) 
- Add `AGCOUCH_MCP_TOKEN=yupp_dev_{...}` to your environment variables
- Also, please set the Network Access to "Full", so claude code can access our MCP server.

To use Agcouch MCP server on Claude Cowork:
- Go to Settings -> Connectors;
- Click "Add Custom Connector" button
- Enter "Agcouch MCP" as the name.
- Enter `https://agcouch-mcp-oauth.example.com/mcp` on the Remote MCP server URL field
- After a little bit when the "Connect" button lights up, click the button and follow the steps to finish oauth setup
- Optional: if you want to setup staging server instead, please use `https://agcouch-mcp-oauth-staging.example.com/mcp`

To set up the staging MCP server to debug staging issues, please refer to [Staging Server Setup](#staging-server-setup).

## Architecture

```
Engineer's Cloud Agent (Claude Code Web)    or    Browser-based MCP Client
         ↓                                              ↓
    HTTPS with Bearer token                    HTTPS with OAuth
         ↓                                              ↓
    ┌───────────────────────────────────────────────────┐
    │           Cloud Run (yupp-mind:mcp mode)          │
    │                                                   │
    │   MCP_SERVER_MODE=DEV_TOKEN  │  MCP_SERVER_MODE=OAUTH
    │   DevTokenAuthMiddleware     │  GoogleProvider (FastMCP)
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
```bash
curl -X POST https://agcouch-mcp.example.com/mcp/tools/search_gcp_logs \
  -H "Authorization: Bearer yupp_dev_xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "arguments": {
      "query": "severity=ERROR AND labels.user_id=\"abc123\"",
      "hours_back": 24
    }
  }'
```

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

The MCP server supports two authentication modes, controlled by the `MCP_SERVER_MODE` environment variable:

### DEV_TOKEN Mode (Default)

Uses Bearer tokens created via CLI for engineer access:

```
Authorization: Bearer yupp_dev_<token>
```

Tokens are:
- **Scoped to engineer email** - Each token belongs to a specific Yupp engineer
- **Hashed with bcrypt** - Never stored in plaintext
- **Optionally expiring** - Can set expiration date
- **Revocable** - Can be revoked at any time with reason

This mode is ideal for:
- Local development
- CI/CD pipelines
- Automated agents (Claude Code, Cursor)

### OAUTH Mode

Uses Google OAuth via FastMCP's GoogleProvider for browser-based authentication:

- Users authenticate via Google OAuth flow
- Email domain validation ensures only allowed domains (e.g., `example.com`) can access
- Tokens are stored encrypted in Redis
- No manual token management required

This mode is ideal for:
- Web-based MCP clients
- Interactive browser sessions
- Self-service access for authorized users

### Mode Configuration

Set the mode via environment variable:

```bash
# DevToken mode (default)
MCP_SERVER_MODE=DEV_TOKEN

# OAuth mode
MCP_SERVER_MODE=OAUTH
```

For OAuth mode, additional configuration is required:
- `MCP_OAUTH_GOOGLE_CLIENT_ID` - Google OAuth client ID
- `MCP_OAUTH_GOOGLE_CLIENT_SECRET` - Google OAuth client secret
- `MCP_OAUTH_JWT_SIGNING_KEY` - JWT signing key for token management
- `MCP_OAUTH_STORAGE_ENCRYPTION_KEY` - Fernet key for Redis token encryption

## Audit Logging

Every tool invocation is logged to `mcp_audit_logs` table with:
- **Who**: Engineer email and token type (DEV_TOKEN or OAUTH)
- **What**: Tool name and parameters
- **Result**: Success/failure, error message, result summary
- **When**: Timestamp
- **Performance**: Execution time in milliseconds
- **Context**: IP address, user agent, OAuth callback URL (if applicable)

Audit logging works identically for both authentication modes, ensuring complete traceability regardless of how users authenticate.

## Token Management

**IMPORTANT**: Token management commands should be executed through GitHub Actions workflows, not locally. This ensures all token operations write to the production database with proper audit trails.

### Requesting a Token

To request a new MCP token, trigger the GitHub Actions workflow with your details. The workflow will execute the token creation command against the production database.

**GitHub Actions Command:**
python -m ypl.cli mcp-create-token \
  --email engineer@example.com \
  --description "Token for debugging prod issues" \
  --expires-days 90


### List Tokens

**GitHub Actions Command:**
```bash
# All tokens
python -m ypl.cli mcp-list-tokens

# Filter by engineer
python -m ypl.cli mcp-list-tokens --email engineer@example.com

# Only active tokens
python -m ypl.cli mcp-list-tokens --active-only
```

### Revoke a Token

**GitHub Actions Command:**
```bash
python -m ypl.cli mcp-revoke-token <token-id> \
  --revoked-by <email@example.com> \
  --reason "Engineer left company"
```

### View Audit Logs

**GitHub Actions Command:**
```bash
# Last 24 hours
python -m ypl.cli mcp-audit-log

# Filter by engineer
python -m ypl.cli mcp-audit-log --email engineer@example.com

# Filter by tool
python -m ypl.cli mcp-audit-log --tool search_gcp_logs

# Last 7 days, limit 100
python -m ypl.cli mcp-audit-log --hours 168 --limit 100
```

### Usage Statistics

**GitHub Actions Command:**
```bash
# Last 7 days
python -m ypl.cli mcp-stats

# Last 30 days
python -m ypl.cli mcp-stats --days 30
```

**Output:**
```
MCP Usage Statistics (Last 7 days)

============================================================
Total Calls: 1234
  Successful: 1180
  Failed: 54
Average Execution Time: 245ms

Top Engineers:
  alice@example.com: 450 calls
  bob@example.com: 320 calls
  charlie@example.com: 280 calls

Top Tools:
  search_gcp_logs: 720 calls
  query_database: 350 calls
  search_vercel_logs: 164 calls
```

## Agent Configuration

### Project Configuration (`.mcp.json`)

The repository includes a project-level `.mcp.json` that configures the agcouch-mcp server for all engineers and CI/agent workflows:

```json
{
  "mcpServers": {
    "agcouch-mcp-server": {
      "type": "http",
      "url": "https://agcouch-mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${AGCOUCH_MCP_TOKEN}"
      }
    },
    "agcouch-mcp-server-staging": {
      "type": "http",
      "url": "https://agcouch-mcp-staging.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${AGCOUCH_MCP_TOKEN_STAGING}"
      },
      "disabled": true
    }
  }
}
```

The `${AGCOUCH_MCP_TOKEN}` placeholder is expanded from environment variables at runtime. See setup instructions below.

### Staging Server Setup

A staging MCP server is available for debugging staging-specific issues. It's disabled by default to avoid confusion with the production server.

**Step 1: Create a staging token**

Run the `Create MCP Token` GitHub Action to create a token for the staging environment. Select "staging" as the target environment.

**Step 2: Configure your staging token**

Add to your shell profile (`~/.zshrc` or `~/.bashrc`):
```bash
export AGCOUCH_MCP_TOKEN_STAGING="yupp_dev_YOUR_STAGING_TOKEN_HERE"
```

Then restart your terminal or run `source ~/.zshrc`.

**Step 3: Enable the staging server**

In Claude Code, run `/mcp` and enable `agcouch-mcp-server-staging` for your session.

### Engineer Setup (Local/Interactive Mode)

For local development, each engineer needs to configure their personal MCP token:

**Step 1: Request a token**

Run the GitHub Actions workflow or contact a team lead to create a token for your email.

**Step 2: Configure your token (choose one option)**

**Option A: Environment variable (recommended)**

Add to your shell profile (`~/.zshrc` or `~/.bashrc`):
```bash
export AGCOUCH_MCP_TOKEN="yupp_dev_YOUR_TOKEN_HERE"
```

Then restart your terminal or run `source ~/.zshrc`.

**Option B: User-scoped Claude Code config**

Add the server directly to your personal Claude Code configuration (stored in `~/.claude.json`):
```bash
claude mcp add --transport http agcouch-mcp-server --scope user \
  https://agcouch-mcp.example.com/mcp \
  --header "Authorization: Bearer yupp_dev_YOUR_TOKEN_HERE"
```

This user-scoped config takes precedence over the project `.mcp.json` and keeps your token private.

**Step 3: Verify**

Start Claude Code and check that `agcouch-mcp-server` appears in your available MCP servers:
```bash
claude mcp list
```

### CI/Agent Mode Setup (GitHub Actions)

For automated workflows and agent mode, configure the token via GitHub Secrets:

**Step 1: Add the secret**

1. Go to repository Settings → Secrets and variables → Actions
2. Add a new secret: `AGCOUCH_MCP_TOKEN` with the token value

**Step 2: Reference in workflow**

```yaml
jobs:
  claude-agent:
    runs-on: ubuntu-latest
    env:
      AGCOUCH_MCP_TOKEN: ${{ secrets.AGCOUCH_MCP_TOKEN }}
    steps:
      - uses: actions/checkout@v4
      - name: Run Claude Code
        run: claude -p "Your prompt here"
```

The `.mcp.json` in the repository will automatically use the `AGCOUCH_MCP_TOKEN` environment variable.

### MCP Protocol Details

**Endpoint:**
- `POST /mcp/` - Streamable HTTP transport for bidirectional JSON-RPC messages

**Required Headers:**
- `Authorization: Bearer <token>`
- `Content-Type: application/json`
- `Accept: application/json`

The server uses Streamable HTTP transport (modern replacement for SSE):
- Single endpoint instead of separate SSE + messages endpoints
- Better scalability with stateless design
- Full bidirectional communication via HTTP streaming

### REST API (for testing)

For simpler integrations or testing, REST convenience endpoints are also available:

```bash
# List available tools
curl https://agcouch-mcp.example.com/mcp/tools \
  -H "Authorization: Bearer yupp_dev_xxx"

# Invoke a tool
curl -X POST https://agcouch-mcp.example.com/mcp/tools/search_gcp_logs \
  -H "Authorization: Bearer yupp_dev_xxx" \
  -H "Content-Type: application/json" \
  -d '{"arguments": {"query": "severity=ERROR"}}'
```

**Note:** The REST endpoints are a convenience layer. Use the `/mcp` endpoint for full MCP protocol compliance.

### Documentation References

- [Claude Code MCP Configuration](https://docs.anthropic.com/en/docs/claude-code/mcp)
- [Claude Code Settings](https://docs.anthropic.com/en/docs/claude-code/settings)
- [Claude Code Headless/Agent Mode](https://docs.anthropic.com/en/docs/claude-code/headless)

## Deployment

### Cloud Run Configuration

```bash
# Build and deploy
gcloud run deploy agcouch-mcp-server \
  --source . \
  --region us-central1 \
  --set-env-vars="BACKEND_OPERATING_MODE=mcp" \
  --service-account yupp-mcp-server@yupp-llms.iam.gserviceaccount.com
```

### Environment Variables

Required:
- `BACKEND_OPERATING_MODE=mcp` - Enables MCP server mode
- `GCP_PROJECT_ID=yupp-llms` - Google Cloud project
- `POSTGRES_HOST`, `POSTGRES_PASSWORD`, etc. - Database credentials

Authentication Mode:
- `MCP_SERVER_MODE=DEV_TOKEN` - Use DevToken authentication (default)
- `MCP_SERVER_MODE=OAUTH` - Use Google OAuth authentication

OAuth Mode (required when `MCP_SERVER_MODE=OAUTH`):
- `MCP_OAUTH_GOOGLE_CLIENT_ID` - Google OAuth client ID
- `MCP_OAUTH_GOOGLE_CLIENT_SECRET` - Google OAuth client secret
- `MCP_OAUTH_JWT_SIGNING_KEY` - JWT signing key (generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
- `MCP_OAUTH_STORAGE_ENCRYPTION_KEY` - Fernet encryption key (generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
- `REDIS_URL` - Redis URL for OAuth token storage

Optional:
- `ALLOWED_MCP_EMAIL_DOMAINS=example.com` - Restrict access to specific email domains (applies to both modes)

## Security Considerations

### Token Security
- Tokens are hashed with bcrypt (never stored plaintext)
- Each token tied to engineer email for accountability
- Tokens can be revoked instantly
- Optional expiration dates

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

### "Invalid or expired token"
- Check token hasn't been revoked: `python -m ypl.cli mcp-list-tokens`
- Verify token hasn't expired
- Ensure correct format: `Authorization: Bearer yupp_dev_xxx`

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
cd /path/to/yupp-mind
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
├── auth_dev_token.py  # DevToken authentication (create, validate, revoke, middleware)
├── auth_oauth.py      # OAuth authentication (GoogleProvider with domain validation)
├── mcp_tools.py       # Tool implementations (GCP logs, DB queries)
├── tasks.py           # TaskIQ tasks for async token management
├── entrypoint.sh      # Production startup script
└── README.md
```

The MCP protocol is implemented using:
- **FastMCP** - High-level MCP server framework (tools registered via decorators)
- **Streamable HTTP transport** - Modern replacement for SSE (single `/mcp` endpoint)
- **Starlette** - Lightweight ASGI framework for routing and middleware
- **Mode-based authentication**:
  - DEV_TOKEN mode: `DevTokenAuthMiddleware` validates yupp_dev_* tokens
  - OAUTH mode: FastMCP's GoogleProvider handles OAuth flow

```
MCP_SERVER_MODE selection:

DEV_TOKEN mode                      OAUTH mode
      │                                  │
      ▼                                  ▼
DevTokenAuthMiddleware          FastMCP GoogleProvider
   (Starlette)                    (FastMCP internal)
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

# Test token creation (manages its own DB session)
from ypl.mcp_server.auth_dev_token import create_token

async def test_create():
    token, db_token = await create_token(
        email="test@example.com",
        description="Test token",
    )
    print(f"Token: {token}")

asyncio.run(test_create())
```

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

### `mcp_dev_tokens` table
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

### `mcp_audit_logs` table
```sql
-- Enum types
CREATE TYPE mcpauditlogstatus AS ENUM ('SUCCESS', 'FAILED');
CREATE TYPE mcptokentype AS ENUM ('DEV_TOKEN', 'OAUTH');

CREATE TABLE mcp_audit_logs (
    mcp_audit_log_id UUID PRIMARY KEY,
    -- Who (supports both DevToken and OAuth)
    mcp_dev_token_id UUID REFERENCES mcp_dev_tokens(mcp_dev_token_id),  -- NULL for OAuth
    email VARCHAR(255),              -- User email (from token or OAuth claims)
    callback_url TEXT,               -- OAuth callback origin (NULL for DevToken)
    token_type mcptokentype NOT NULL DEFAULT 'DEV_TOKEN',
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
- Check audit logs: `python -m ypl.cli mcp-audit-log`
- Review token status: `python -m ypl.cli mcp-list-tokens`
- Contact platform team for access issues
