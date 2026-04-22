# Artifact Viewer

Read-only web UI for AHS textual artifacts. Runs as a tiny Starlette app
behind Google OAuth; fetches everything from AHS at request time and
renders markdown / HTML / plain text / attached images inline. Intended
deployment: `artifacts.agcouch.com`.

## What it does

- Google OAuth (domain + email allowlist)
- Routes that mirror AHS's REST layout:
  - `/` — recent artifacts + search box
  - `/search?q=…` — substring match over title / description / slug / attachment filenames
  - `/artifacts/{uuid}` — rendered artifact
  - `/artifacts/by-slug/{slug}` — latest version
  - `/artifacts/by-slug/{slug}/v/{N}` — pinned version
  - `/artifacts/by-slug/{slug}/versions` — version history
  - `/artifacts/{uuid}/attachments/{filename}` — attachment bytes (for `<img>`)
- Rendering:
  - `text/markdown` → `markdown-it-py` → `bleach.clean()` with a strict allowlist. `attachment:foo.png` shorthand is rewritten to point at the viewer.
  - `text/html` → sandboxed iframe (`sandbox="allow-popups"`, no same-origin, no scripts) so agent-authored HTML can't touch the viewer cookies.
  - `text/plain` → `<pre>` wrap.
- Attachment rendering: images inline, everything else as download links.

## Stack

| Dependency | Purpose |
|---|---|
| `starlette` + `uvicorn` | async HTTP (no FastAPI — this is pure HTML-over-HTTP) |
| `httpx` | async proxy to AHS |
| `authlib` | Google OAuth |
| `itsdangerous` (via Starlette) | signed session cookies — no Redis |
| `jinja2` | 6 templates |
| `markdown-it-py` + `bleach` | safe markdown → HTML |
| `pydantic-settings` | env var loading |

Resource profile (single uvicorn worker): ~40 MB RSS, ~200 ms cold start. No database, no Redis.

## Config

Config is env-driven. See [`.env.example`](.env.example) for the full
list. Every viewer-specific variable is prefixed with `VIEWER_` so the
sub-app can share the monolith's `.env` file without colliding with
AHS / SAG / MCP settings.

On the monolith VM the viewer reads `/opt/yupp-agent/.env` directly
(via its systemd unit's `EnvironmentFile=`). **You only need to append
`VIEWER_*` entries to that file.** The shared
`AGENT_HARNESS_SERVICE_API_KEY` is reused from what AHS already has.

Required keys:

| Var | Required | Notes |
|---|---|---|
| `AGENT_HARNESS_SERVICE_API_KEY` | ✓ (shared) | Already set for AHS — viewer reads the same value |
| `VIEWER_GOOGLE_CLIENT_ID` | ✓ | Google Cloud Console OAuth client ID |
| `VIEWER_GOOGLE_CLIENT_SECRET` | ✓ | Paired secret |
| `VIEWER_OAUTH_REDIRECT_URL` | ✓ | Must match the Cloud Console authorized URI exactly |
| `VIEWER_SESSION_SECRET_KEY` | ✓ | 32+ random bytes; rotate to invalidate all sessions |
| `VIEWER_ALLOWED_EMAIL_DOMAINS` | optional | Default `agcouch.com` |
| `VIEWER_AHS_BASE_URL` | optional | Default `https://ahs.agcouch.com` |
| `VIEWER_HOST`, `VIEWER_PORT` | optional | Default `127.0.0.1:8095` |

## Run locally (dev)

```bash
cd apps/artifact-viewer
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

cp .env.example .env   # fill in secrets
# For local HTTP development:
#   VIEWER_SESSION_COOKIE_SECURE=false
#   VIEWER_OAUTH_REDIRECT_URL=http://127.0.0.1:8095/auth/callback
artifact-viewer
# or: uvicorn artifact_viewer.app:app --reload --port 8095
```

Open http://127.0.0.1:8095 — you'll be redirected to Google, then back to the home page.

### Tests

```bash
pytest
ruff check .
mypy .
```

## Deploy to the monolith VM

The viewer is fully wired into the repo's install/deploy scripts:

- **First-time install** (also does ahs-mono + ahs-streamlit):
  ```bash
  curl -fsSL https://raw.githubusercontent.com/yupp-ai/yupp-agent/main/deploy/bare-metal/install.sh | sudo bash
  ```
  This creates `apps/artifact-viewer/.venv` in-tree, `pip install -e`'s
  the sub-app into it, and registers the systemd unit — but does
  **not** start it yet (you need to add `VIEWER_*` entries to `.env`
  first).

- **Rolling deploys after install**:
  ```bash
  sudo bash /opt/yupp-agent/deploy/bare-metal/deploy-latest.sh
  ```
  This pulls the repo, reinstalls both the monolith deps and
  `apps/artifact-viewer/` (cheap no-op if nothing changed), syncs any
  updated unit files, and restarts all three services
  (`ahs-mono`, `ahs-streamlit`, `artifact-viewer`).

- **After install, one-time config**:
  ```bash
  sudo -u ahs nano /opt/yupp-agent/.env   # add VIEWER_* entries
  sudo systemctl start artifact-viewer
  sudo systemctl status artifact-viewer
  curl http://localhost:8095/healthz      # → {"ok": true}
  ```

- **Cloudflare exposure**: the tunnel config at
  `deploy/cloudflared/config.yml` already has an `artifacts.*` ingress
  block pointing at `127.0.0.1:8095`. On your DNS side:
  ```bash
  sudo -u ahs -H cloudflared tunnel route dns yupp-agent artifacts.agcouch.com
  sudo systemctl restart cloudflared
  ```

## Security notes

- The AHS API key is only ever held by this server — the browser never sees it.
- Agent-authored HTML renders inside an iframe with `sandbox="allow-popups allow-popups-to-escape-sandbox"`. No scripts, no same-origin, no form submission, no access to parent cookies.
- Markdown is rendered with `html: false` then sanitized with `bleach` against a conservative tag allowlist.
- Session cookies are HttpOnly, SameSite=Lax, and (in prod) Secure.
- The middleware refuses everything outside `/auth/*`, `/healthz`, and `/static/*` unless the session holds an allowed email.

## What this intentionally does not do (yet)

- Full-text search over artifact body (the AHS endpoint doesn't expose it).
- Admin operations (archive, unarchive).
- Version diffing.
- Multi-tenant access control — it's a single org allowlist for now.
