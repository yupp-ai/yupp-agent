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

All env vars are documented in [`.env.example`](.env.example). The ones you must set:

- `AHS_API_KEY` — same shared secret SAG uses to talk to AHS
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` — from Google Cloud Console
- `SESSION_SECRET_KEY` — 32+ random bytes; rotate to invalidate all sessions
- `ALLOWED_EMAIL_DOMAINS` — default `agcouch.com`

## Run locally

```bash
cd apps/artifact-viewer
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

cp .env.example .env   # fill in secrets
# For local HTTP development:
#   SESSION_COOKIE_SECURE=false
#   OAUTH_REDIRECT_URL=http://127.0.0.1:8095/auth/callback
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

The systemd unit in [`deploy/artifact-viewer.service`](deploy/artifact-viewer.service) assumes:

- Code lives in `/opt/artifact-viewer` (a clone of this repo, or just this sub-app)
- Virtualenv in `/opt/artifact-viewer/.venv`
- `.env` in `/opt/artifact-viewer/.env`
- Run as the `ahs` user

First-time install on the VM:

```bash
sudo mkdir -p /opt/artifact-viewer
sudo chown ahs:ahs /opt/artifact-viewer
sudo -u ahs git clone --depth 1 https://github.com/yupp-ai/yupp-agent.git /tmp/yupp-agent
sudo -u ahs cp -r /tmp/yupp-agent/apps/artifact-viewer/. /opt/artifact-viewer/
sudo -u ahs python3.12 -m venv /opt/artifact-viewer/.venv
sudo -u ahs /opt/artifact-viewer/.venv/bin/pip install -e /opt/artifact-viewer
sudo -u ahs cp /opt/artifact-viewer/.env.example /opt/artifact-viewer/.env
# edit /opt/artifact-viewer/.env
sudo cp /opt/artifact-viewer/deploy/artifact-viewer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now artifact-viewer
```

Then point `artifacts.agcouch.com` at `127.0.0.1:8095` via your reverse proxy (Cloudflare tunnel, nginx, whatever fronts the VM). No TLS termination in-process.

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
