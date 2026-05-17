#!/usr/bin/env bash
# deploy/mac/install.sh
#
# Fully guided, interactive installer for the yupp-agent one-box on macOS.
#
# What this does
# --------------
# Brings up the full suite in Docker (AHS+SAG mono, Streamlit, Artifact Viewer,
# Postgres, Redis) with the host filesystem mounted into the containers, and
# optionally wires the laptop to the public internet via a Cloudflare tunnel
# with Google-OAuth-gated Streamlit and Artifact Viewer endpoints.
#
# Run from the repo root:
#
#     bash deploy/mac/install.sh
#
# Re-runnable.  Each step short-circuits when it has already been done; the
# sentinel file ./ahs-data/.mac-install-done marks a fully successful run.
#
# Non-interactive overrides (env vars):
#   SKIP_CLOUDFLARE=1      Skip the tunnel step entirely (Streamlit + Viewer
#                          stay localhost-only; SAG won't receive Slack events).
#   SKIP_OAUTH=1           Skip the Google OAuth step (Streamlit + Viewer
#                          remain unauthenticated; only safe localhost-only).
#   CF_APEX_DOMAIN=…       Apex domain (e.g. tian.dev).  When set, the script
#                          uses agent.<apex>, agent-ui.<apex>, artifacts.<apex>
#                          without prompting.
#   POSTGRES_PASSWORD=…    Override the auto-generated Postgres password.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

DATA_DIR="${DATA_DIR:-$REPO_ROOT/ahs-data}"
SENTINEL="$DATA_DIR/.mac-install-done"
ENV_FILE="$REPO_ROOT/.env"
ENV_EXAMPLE="$REPO_ROOT/.env.example"
COMPOSE_FILE="$REPO_ROOT/docker-compose.one-box.yml"
COMPOSE="docker compose -f $COMPOSE_FILE"
CLOUDFLARED_DIR="$HOME/.cloudflared"
CLOUDFLARED_CONFIG="$CLOUDFLARED_DIR/config.yml"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'; BOLD=$'\033[1m'; NC=$'\033[0m'

info()  { echo -e "${GREEN}[mac-install]${NC} $*"; }
warn()  { echo -e "${YELLOW}[mac-install]${NC} $*"; }
error() { echo -e "${RED}[mac-install]${NC} $*" >&2; exit 1; }

banner() {
    echo
    echo -e "${BOLD}${BLUE}============================================================${NC}"
    echo -e "${BOLD}${BLUE}  $*${NC}"
    echo -e "${BOLD}${BLUE}============================================================${NC}"
    echo
}

step() {
    # step <current> <total> <title>
    echo
    echo -e "${BOLD}${BLUE}━━━ Step $1/$2: $3 ━━━${NC}"
    echo
}

prompt_yes_no() {
    # prompt_yes_no "Question" [default-y-or-n]
    local prompt="$1" default="${2:-y}"
    local hint="[Y/n]"; [[ "$default" == "n" ]] && hint="[y/N]"
    local answer
    read -rp "$prompt $hint " answer </dev/tty
    answer="${answer:-$default}"
    [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}

prompt_value() {
    # prompt_value "Question" "default"
    local prompt="$1" default="${2:-}"
    local answer
    if [[ -n "$default" ]]; then
        read -rp "$prompt [$default] " answer </dev/tty
        echo "${answer:-$default}"
    else
        read -rp "$prompt " answer </dev/tty
        echo "$answer"
    fi
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || error "$1 not found on PATH.  $2"
}

generate_secret() {
    # 32 url-safe bytes, no trailing newline.
    python3 -c "import secrets; print(secrets.token_urlsafe(32))"
}

generate_fernet_key() {
    # 32 url-safe bytes base64-encoded — Fernet-compatible.  Required for
    # SLACK_AGENT_GW_ENCRYPTION_KEY and MCP_OAUTH_STORAGE_ENCRYPTION_KEY;
    # secrets.token_urlsafe(32) is the wrong length/encoding for Fernet.
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
}

pgdata_volume_exists() {
    # Compose normally name-prefixes named volumes with the project name
    # (basename of the directory).  Check both common forms — the prefixed
    # form Compose creates and the bare name in case the project_name was
    # set explicitly.
    local project_name
    project_name="$(basename "$REPO_ROOT")"
    docker volume inspect "${project_name}_pgdata" >/dev/null 2>&1 && return 0
    docker volume inspect "pgdata" >/dev/null 2>&1 && return 0
    return 1
}

env_get() {
    # Read a key from $ENV_FILE; print empty string if absent.
    local key="$1"
    [[ -f "$ENV_FILE" ]] || { echo ""; return; }
    awk -F= -v k="$key" '$1==k {sub(/^[^=]*=/, ""); print; exit}' "$ENV_FILE"
}

env_set() {
    # Idempotent set "KEY=VALUE" in $ENV_FILE.  Quotes are NOT added — caller
    # supplies them when the value contains shell metacharacters.
    local key="$1" value="$2"
    touch "$ENV_FILE"
    if grep -qE "^${key}=" "$ENV_FILE"; then
        # macOS sed needs `-i ''` for in-place edits; use a backup-suffix
        # trick that works on both BSD and GNU sed.
        local tmp; tmp="$(mktemp)"
        awk -F= -v k="$key" -v v="$value" '
            BEGIN { OFS="=" }
            $1==k { print k "=" v; replaced=1; next }
            { print }
            END { if (!replaced) print k "=" v }
        ' "$ENV_FILE" > "$tmp"
        mv "$tmp" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}

banner "yupp-agent — macOS one-box installer"

if [[ "$(uname)" != "Darwin" ]]; then
    error "This installer is macOS-only.  For Linux see deploy/bare-metal/install.sh."
fi

if [[ -f "$SENTINEL" ]]; then
    warn "Sentinel $SENTINEL exists — previous run completed."
    if ! prompt_yes_no "Re-run the installer from scratch?" "n"; then
        info "Nothing to do."
        exit 0
    fi
    rm -f "$SENTINEL"
fi

TOTAL_STEPS=8

# ---------------------------------------------------------------------------
step 1 $TOTAL_STEPS "Toolchain check"
# ---------------------------------------------------------------------------
require_cmd brew   "Install Homebrew first: https://brew.sh"
require_cmd docker "Install Docker Desktop: https://www.docker.com/products/docker-desktop"
require_cmd python3 "Install Python 3.12: brew install python@3.12"

if ! docker info >/dev/null 2>&1; then
    error "Docker daemon is not running.  Start Docker Desktop and re-run."
fi

# `docker compose` (v2) ships with Docker Desktop; verify it.
if ! docker compose version >/dev/null 2>&1; then
    error "docker compose v2 not available.  Update Docker Desktop."
fi

# jq is used to manipulate JSON env values further down.
if ! command -v jq >/dev/null 2>&1; then
    info "Installing jq via brew..."
    brew install jq
fi

PYTHON_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$PYTHON_VERSION" != "3.12" ]]; then
    warn "Detected Python $PYTHON_VERSION; the setup wizard expects 3.12."
    warn "  brew install python@3.12 && brew link --force python@3.12"
fi

if ! command -v poetry >/dev/null 2>&1; then
    info "Installing Poetry 1.8.5 via the official installer..."
    curl -sSL https://install.python-poetry.org | python3 - --version 1.8.5
    export PATH="$HOME/.local/bin:$PATH"
fi
require_cmd poetry "Re-open your shell or add \$HOME/.local/bin to PATH."

info "All required tools present."

# ---------------------------------------------------------------------------
step 2 $TOTAL_STEPS "Workspace directories + agent repo seed"
# ---------------------------------------------------------------------------
mkdir -p "$DATA_DIR/sessions" "$DATA_DIR/repos" "$DATA_DIR/agent_memories" "$DATA_DIR/artifacts"
info "Host bind-mount roots ready under $DATA_DIR/"
info "  → /data/ahs/{sessions,repos,agent_memories,artifacts}/ inside containers"

# AHS resolves agent worktrees from AHS_REPOS_DIR (default
# /data/ahs/repos/).  list_available_repos / request_write_access return
# "repo not found" until at least one repo is present, so seed yupp-agent
# the way deploy/bare-metal/install.sh does (DEFAULT_AGENT_REPOS loop).
DEFAULT_AGENT_REPOS="${DEFAULT_AGENT_REPOS:-yupp-ai/yupp-agent}"
for repo_spec in $DEFAULT_AGENT_REPOS; do
    repo_name="${repo_spec##*/}"
    repo_name="${repo_name%.git}"
    target="$DATA_DIR/repos/$repo_name"
    if [[ -d "$target/.git" ]]; then
        info "Agent repo already cloned: $target"
        continue
    fi
    if [[ "$repo_spec" == *"://"* || "$repo_spec" == *"@"* ]]; then
        clone_url="$repo_spec"
    else
        clone_url="https://github.com/${repo_spec}.git"
    fi
    info "Cloning default agent repo $repo_spec → $target..."
    if git clone --depth 50 "$clone_url" "$target"; then
        info "  ✓ $repo_name"
    else
        warn "  ✗ Failed to clone $repo_spec.  Clone manually later:"
        warn "      git clone $clone_url $target"
    fi
done

# ---------------------------------------------------------------------------
step 3 $TOTAL_STEPS "Generate .env"
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    info "Created .env from .env.example (mode 0600)"
else
    chmod 600 "$ENV_FILE"
    info ".env already exists — leaving existing values in place; appending missing keys."
fi

# Mac/Docker-flavoured defaults — only write if not already set.  Note:
# AHS_DATA_DIR is deliberately NOT written to .env — `docker-compose.one-box.yml`
# sets it on the `app` service environment block, where it belongs.  Writing it
# to a shared .env would poison the host (poetry-dotenv-plugin, the setup
# wizard, future dev tooling) by resolving AHS_REPOS_DIR / AHS_SESSIONS_DIR to
# /data/ahs, which doesn't exist on macOS.
[[ -z "$(env_get ENVIRONMENT)" ]]                       && env_set ENVIRONMENT                       "selfhosted"
[[ -z "$(env_get SANDBOX_ENABLED)" ]]                   && env_set SANDBOX_ENABLED                   "false"
[[ -z "$(env_get AHS_MONO_ENABLE_GATEWAY_SERVICE)" ]]   && env_set AHS_MONO_ENABLE_GATEWAY_SERVICE   "true"
[[ -z "$(env_get GATEWAY_SLACK_ENABLED)" ]]             && env_set GATEWAY_SLACK_ENABLED             "true"
[[ -z "$(env_get USE_GOOGLE_CLOUD_LOGGING)" ]]          && env_set USE_GOOGLE_CLOUD_LOGGING          "false"
[[ -z "$(env_get DISABLE_WRITE_GOOGLE_CLOUD_METRICS)" ]] && env_set DISABLE_WRITE_GOOGLE_CLOUD_METRICS "true"

# Postgres credentials — generate a strong password on first run.  Refuse to
# regenerate the password if the pgdata named volume already exists, because
# Postgres only honours POSTGRES_PASSWORD on first initdb; rewriting it on
# top of an existing role yields the classic opaque `28P01` desync.
[[ -z "$(env_get POSTGRES_USER)" ]] && env_set POSTGRES_USER "postgres"
[[ -z "$(env_get POSTGRES_DB)" ]]   && env_set POSTGRES_DB   "yadb"
_existing_pw="$(env_get POSTGRES_PASSWORD)"
if [[ -z "$_existing_pw" ]] || [[ "$_existing_pw" == "postgres" ]] || [[ "$_existing_pw" == "changeme_before_deployment" ]]; then
    if pgdata_volume_exists; then
        error "Postgres data volume already exists but .env has no usable POSTGRES_PASSWORD.
        Generating a new password now would desync the persisted role.
        Either restore the original password to .env, or run
            $COMPOSE down -v
        to wipe the pgdata volume and start fresh."
    fi
    env_set POSTGRES_PASSWORD "${POSTGRES_PASSWORD:-$(generate_secret)}"
    info "Wrote a strong POSTGRES_PASSWORD (initdb will pick it up on first start)."
fi

# Internal AHS API key.
if [[ -z "$(env_get AGENT_HARNESS_SERVICE_API_KEY)" ]]; then
    env_set AGENT_HARNESS_SERVICE_API_KEY "$(generate_secret)"
fi

# Viewer session secret — independent of OAuth client; the viewer needs it
# even with auth disabled because Starlette's session middleware always loads.
if [[ -z "$(env_get VIEWER_SESSION_SECRET_KEY)" ]]; then
    env_set VIEWER_SESSION_SECRET_KEY "$(generate_secret)"
fi

# Streamlit cookie secret.
if [[ -z "$(env_get GOOGLE_AUTH_COOKIE_SECRET)" ]]; then
    env_set GOOGLE_AUTH_COOKIE_SECRET "$(generate_secret)"
fi

# Backend cross-service secret — backend/config.py falls back to a per-process
# token_urlsafe(32) otherwise, which means user sessions die on every restart.
if [[ -z "$(env_get SECRET_KEY)" ]]; then
    env_set SECRET_KEY "$(generate_secret)"
fi

# X-API-Key (and rotation slot) — backend/config.py needs both for header auth.
if [[ -z "$(env_get X_API_KEY)" ]]; then
    _xkey="$(generate_secret)"
    env_set X_API_KEY           "$_xkey"
    env_set X_API_KEY_SECONDARY "$_xkey"
fi

# MCP OAuth JWT signing — opaque random secret is fine.
if [[ -z "$(env_get MCP_OAUTH_JWT_SIGNING_KEY)" ]]; then
    env_set MCP_OAUTH_JWT_SIGNING_KEY "$(generate_secret)"
fi

# Fernet-format keys — wrong length/encoding for token_urlsafe(32); use
# Fernet.generate_key() so cryptography accepts them.
if [[ -z "$(env_get SLACK_AGENT_GW_ENCRYPTION_KEY)" ]]; then
    env_set SLACK_AGENT_GW_ENCRYPTION_KEY "$(generate_fernet_key)"
fi
if [[ -z "$(env_get MCP_OAUTH_STORAGE_ENCRYPTION_KEY)" ]]; then
    env_set MCP_OAUTH_STORAGE_ENCRYPTION_KEY "$(generate_fernet_key)"
fi

chmod 600 "$ENV_FILE"
info ".env updated (mode 0600)."

# ---------------------------------------------------------------------------
step 4 $TOTAL_STEPS "Cloudflare tunnel (optional but required for SAG)"
# ---------------------------------------------------------------------------
SKIP_CF="${SKIP_CLOUDFLARE:-0}"
if [[ "$SKIP_CF" != "1" ]]; then
    if prompt_yes_no "Set up a Cloudflare tunnel so this laptop has a public hostname?" "y"; then
        if ! command -v cloudflared >/dev/null 2>&1; then
            info "Installing cloudflared via brew..."
            brew install cloudflare/cloudflare/cloudflared
        fi
        mkdir -p "$CLOUDFLARED_DIR"

        # Auth — login is interactive and opens a browser.
        if [[ ! -f "$CLOUDFLARED_DIR/cert.pem" ]]; then
            info "Opening a browser for Cloudflare login (cloudflared tunnel login)..."
            cloudflared tunnel login
        else
            info "Existing Cloudflare credentials found at $CLOUDFLARED_DIR/cert.pem"
        fi

        APEX="${CF_APEX_DOMAIN:-}"
        [[ -z "$APEX" ]] && APEX="$(prompt_value 'Cloudflare apex domain (e.g. tian.dev)' '')"
        [[ -z "$APEX" ]] && error "Apex domain is required for the tunnel step."

        AGENT_HOST="agent.$APEX"
        AGENT_UI_HOST="agent-ui.$APEX"
        ARTIFACTS_HOST="artifacts.$APEX"

        TUNNEL_NAME="${TUNNEL_NAME:-yupp-agent}"
        # Parse the JSON output rather than the text-table — cloudflared has
        # shifted text-column ordering across releases (and is moving toward
        # JSON-by-default), so awk-by-column is brittle.
        TUNNEL_UUID="$(cloudflared tunnel list --output json 2>/dev/null \
            | jq -r --arg n "$TUNNEL_NAME" '.[] | select(.name == $n) | .id' \
            | head -n 1)"
        if [[ -z "$TUNNEL_UUID" ]]; then
            info "Creating tunnel $TUNNEL_NAME..."
            cloudflared tunnel create "$TUNNEL_NAME"
            TUNNEL_UUID="$(cloudflared tunnel list --output json 2>/dev/null \
                | jq -r --arg n "$TUNNEL_NAME" '.[] | select(.name == $n) | .id' \
                | head -n 1)"
        else
            info "Tunnel $TUNNEL_NAME already exists ($TUNNEL_UUID); reusing."
        fi
        [[ -z "$TUNNEL_UUID" ]] && error "Could not resolve tunnel UUID for $TUNNEL_NAME."

        # DNS routes.
        for host in "$AGENT_HOST" "$AGENT_UI_HOST" "$ARTIFACTS_HOST"; do
            info "Routing $host → $TUNNEL_NAME"
            cloudflared tunnel route dns "$TUNNEL_NAME" "$host" || warn "DNS route for $host failed (already routed? continuing)"
        done

        # Write ~/.cloudflared/config.yml from the template.  Use 127.0.0.1
        # instead of `localhost` — on macOS `localhost` resolves to ::1
        # first, Docker Desktop's published ports listen on IPv4 0.0.0.0
        # only, and the symptom is "tunnel healthy, every request 502 Bad
        # Gateway with no signal".
        info "Writing $CLOUDFLARED_CONFIG"
        cat > "$CLOUDFLARED_CONFIG" <<EOF
# Generated by deploy/mac/install.sh
tunnel: $TUNNEL_UUID
credentials-file: $CLOUDFLARED_DIR/$TUNNEL_UUID.json

ingress:
  - hostname: $AGENT_HOST
    service: http://127.0.0.1:8090
    originRequest:
      connectTimeout: 10s
  - hostname: $AGENT_UI_HOST
    service: http://127.0.0.1:8501
    originRequest:
      connectTimeout: 10s
  - hostname: $ARTIFACTS_HOST
    service: http://127.0.0.1:8095
    originRequest:
      connectTimeout: 10s
  - service: http_status:404
EOF

        # brew services start cloudflared launches it as a LaunchAgent so it
        # survives reboots.  brew's cloudflared service reads
        # ~/.cloudflared/config.yml automatically.
        info "Starting cloudflared as a brew service..."
        brew services restart cloudflared >/dev/null

        # Stash hostnames in .env for downstream services.
        env_set AGENT_HOST                "$AGENT_HOST"
        env_set AGENT_UI_HOST             "$AGENT_UI_HOST"
        env_set ARTIFACTS_HOST            "$ARTIFACTS_HOST"
        # Tell SAG manifest builders (if anyone ever calls them) the public
        # base URL.  Streamlit + the viewer read their redirect URIs below.
        env_set GATEWAY_BASE_URL          "https://$AGENT_HOST"
        env_set AHS_LIT_BASE_URL          "https://$AGENT_UI_HOST"

        info "Tunnel ready:"
        info "  AHS / SAG          https://$AGENT_HOST"
        info "  Streamlit UI       https://$AGENT_UI_HOST"
        info "  Artifact Viewer    https://$ARTIFACTS_HOST"
    else
        warn "Skipped Cloudflare tunnel — SAG will not receive Slack events without one."
    fi
else
    warn "SKIP_CLOUDFLARE=1 — skipping tunnel setup."
fi

# ---------------------------------------------------------------------------
step 5 $TOTAL_STEPS "Google OAuth (Streamlit + Artifact Viewer)"
# ---------------------------------------------------------------------------
SKIP_OAUTH_FLAG="${SKIP_OAUTH:-0}"
if [[ "$SKIP_OAUTH_FLAG" != "1" ]]; then
    AGENT_UI_HOST_CURR="$(env_get AGENT_UI_HOST)"
    ARTIFACTS_HOST_CURR="$(env_get ARTIFACTS_HOST)"

    if [[ -n "$AGENT_UI_HOST_CURR" && -n "$ARTIFACTS_HOST_CURR" ]]; then
        cat <<EOF

  Create a Google Cloud OAuth 2.0 Web Application client at:
    https://console.cloud.google.com/apis/credentials

  Configure the following Authorized redirect URIs (both required):
    https://$AGENT_UI_HOST_CURR/oauth2callback
    https://$ARTIFACTS_HOST_CURR/auth/callback

EOF
    else
        cat <<EOF

  Create a Google Cloud OAuth 2.0 Web Application client at:
    https://console.cloud.google.com/apis/credentials

  Authorized redirect URIs — fill in whatever hostnames you'll reach the
  Streamlit (8501) and Artifact Viewer (8095) at; the most common pair is:
    http://127.0.0.1:8501/oauth2callback     (Streamlit, localhost-only)
    http://127.0.0.1:8095/auth/callback      (Artifact Viewer, localhost-only)

  Note: Streamlit's auth helper *requires* a redirect URI; if you skipped the
  Cloudflare step and intend to keep it localhost-only, you can still configure
  OAuth using the 127.0.0.1 URIs above.

EOF
    fi

    if prompt_yes_no "Configure Google OAuth now?" "y"; then
        CLIENT_ID="$(prompt_value 'Google OAuth Client ID' "$(env_get GOOGLE_AUTH_CLIENT_ID)")"
        CLIENT_SECRET="$(prompt_value 'Google OAuth Client Secret' "$(env_get GOOGLE_AUTH_CLIENT_SECRET)")"

        env_set GOOGLE_AUTH_CLIENT_ID     "$CLIENT_ID"
        env_set GOOGLE_AUTH_CLIENT_SECRET "$CLIENT_SECRET"
        # Viewer reuses the same client by default — different redirect path.
        env_set VIEWER_GOOGLE_CLIENT_ID     "$CLIENT_ID"
        env_set VIEWER_GOOGLE_CLIENT_SECRET "$CLIENT_SECRET"

        if [[ -n "$AGENT_UI_HOST_CURR" && -n "$ARTIFACTS_HOST_CURR" ]]; then
            env_set STREAMLIT_GOOGLE_AUTH_REDIRECT_URI "https://$AGENT_UI_HOST_CURR/oauth2callback"
            env_set VIEWER_OAUTH_REDIRECT_URL          "https://$ARTIFACTS_HOST_CURR/auth/callback"
            # AHS uses VIEWER_BASE_URL to format outbound artifact links
            # (see ypl/agent_harness_service/artifact_store.py).  Default
            # points at artifacts.agcouch.com, which would break the
            # "share a link with a teammate" outcome.
            env_set VIEWER_BASE_URL                    "https://$ARTIFACTS_HOST_CURR"
            env_set VIEWER_SESSION_COOKIE_SECURE       "true"
        else
            env_set STREAMLIT_GOOGLE_AUTH_REDIRECT_URI "http://127.0.0.1:8501/oauth2callback"
            env_set VIEWER_OAUTH_REDIRECT_URL          "http://127.0.0.1:8095/auth/callback"
            env_set VIEWER_BASE_URL                    "http://127.0.0.1:8095"
            env_set VIEWER_SESSION_COOKIE_SECURE       "false"
        fi
        # VIEWER_AHS_BASE_URL is intentionally NOT set here — it's pinned
        # in docker-compose.one-box.yml (artifact-viewer.environment) to the
        # internal Docker network address (http://app:8090).  Compose's
        # environment: wins over env_file:, so the .env line would silently
        # have no effect.

        chmod 600 "$ENV_FILE"
        info "OAuth keys written to .env."
    else
        warn "Skipped OAuth — Streamlit and Artifact Viewer will start unauthenticated."
        warn "Do NOT expose them via the Cloudflare tunnel until OAuth is configured."
    fi
else
    warn "SKIP_OAUTH=1 — skipping OAuth wiring."
fi

# ---------------------------------------------------------------------------
step 6 $TOTAL_STEPS "Bring up Postgres + Redis"
# ---------------------------------------------------------------------------
info "Starting postgres and redis..."
$COMPOSE up -d postgres redis

info "Waiting for Postgres to become healthy..."
for i in $(seq 1 60); do
    state="$(docker inspect -f '{{.State.Health.Status}}' "$($COMPOSE ps -q postgres)" 2>/dev/null || true)"
    if [[ "$state" == "healthy" ]]; then
        info "Postgres healthy."
        break
    fi
    if [[ "$i" == "60" ]]; then
        error "Postgres never reached healthy.  Check: $COMPOSE logs postgres"
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
step 7 $TOTAL_STEPS "Setup wizard (migrations + admin user)"
# ---------------------------------------------------------------------------
if ! [[ -f "$REPO_ROOT/poetry.lock" ]]; then
    error "poetry.lock missing — are you running from the repo root?"
fi

if ! poetry env info --path >/dev/null 2>&1; then
    info "First-time poetry install — this takes a few minutes."
    poetry install --no-root --without dev
fi

info "Running ypl.mono_server.setup against the dockerized postgres."
# The wizard reads its env from AHS_ENV_PATH (or AHS_DATA_DIR/.env).  Point
# it at the same .env compose injects via env_file: so wizard writes and
# container reads agree on every secret.
# POSTGRES_HOST=localhost:5432 because the wizard runs on the host; Docker
# has published 5432 to 127.0.0.1.
#
# Fail hard on non-zero exit.  The wizard runs migrations, seeds roles, and
# creates the admin user — if any of that fails, the container will later
# crash-loop with no schema.  Soft-fail + sentinel-on-success is the worst
# of both worlds.
export AHS_ENV_PATH="$ENV_FILE"
if ! poetry run python -m ypl.mono_server.setup; then
    error "Setup wizard failed.  Fix the underlying error and re-run.
        Verify Postgres is reachable:        $COMPOSE logs postgres
        Re-run the wizard manually:          AHS_ENV_PATH=$ENV_FILE poetry run python -m ypl.mono_server.setup"
fi

# ---------------------------------------------------------------------------
step 8 $TOTAL_STEPS "Bring up the rest of the stack"
# ---------------------------------------------------------------------------
info "Building images and starting app, streamlit, artifact-viewer..."
$COMPOSE up -d --build app streamlit artifact-viewer

info "Waiting for /health on each service..."
wait_for_url() {
    local label="$1" url="$2"
    for i in $(seq 1 60); do
        if curl -sf "$url" >/dev/null 2>&1; then
            info "$label OK ($url)"
            return 0
        fi
        sleep 2
    done
    warn "$label never responded at $url — check: $COMPOSE logs $(printf '%s' "$label" | tr '[:upper:]' '[:lower:]')"
    return 1
}

wait_for_url "app"             "http://localhost:8090/health"
wait_for_url "streamlit"       "http://localhost:8501/_stcore/health"
wait_for_url "artifact-viewer" "http://localhost:8095/healthz"

touch "$SENTINEL"

banner "Done"

cat <<EOF
${GREEN}yupp-agent one-box is up.${NC}

Local URLs:
  AHS / SAG / MCP            http://localhost:8090
    health                   http://localhost:8090/health
    OpenAPI docs             http://localhost:8090/docs
  Streamlit dashboards       http://localhost:8501
  Artifact Viewer            http://localhost:8095

EOF

if [[ -n "$(env_get AGENT_HOST)" ]]; then
    cat <<EOF
Public URLs (via your Cloudflare tunnel):
  https://$(env_get AGENT_HOST)            — AHS / SAG / MCP
  https://$(env_get AGENT_UI_HOST)         — Streamlit (Google OAuth)
  https://$(env_get ARTIFACTS_HOST)        — Artifact Viewer (Google OAuth)

Slack bot setup — register the bot through Streamlit (Agent Console → Slack
Agents page) and point the Slack app's request_url at:
  https://$(env_get AGENT_HOST)/gw/slack/slack/events

EOF
fi

cat <<EOF
Management:
  Stop                       $COMPOSE down
  Logs                       $COMPOSE logs -f app
  Re-run installer           bash deploy/mac/install.sh

Workspace bind mounts:
  $DATA_DIR/sessions/        /data/ahs/sessions/        in containers
  $DATA_DIR/repos/           /data/ahs/repos/           in containers
  $DATA_DIR/agent_memories/  /data/ahs/agent_memories/  in containers
  $DATA_DIR/artifacts/       /data/ahs/artifacts/       in containers
EOF
