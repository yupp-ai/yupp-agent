#!/usr/bin/env bash
# deploy/bare-metal/install.sh
#
# Fully guided, interactive installer for a single-VM AHS deployment
# ("monolith one-box"). Tested on Ubuntu 22.04/24.04 LTS and Debian 12.
#
# Usage — pick one:
#
#   # Piped from GitHub (recommended — always gets the latest):
#   curl -fsSL https://raw.githubusercontent.com/yupp-ai/yupp-agent/main/deploy/bare-metal/install.sh | sudo bash
#
#   # Or clone first, then run:
#   sudo bash deploy/bare-metal/install.sh
#
# The script is safe to re-run. Each step detects prior state and skips
# what's already done, so if something fails, fix it and re-run.
#
# Non-interactive overrides (skip prompts entirely — useful for CI):
#   INSTALL_CLAUDE_CODE=yes|no       Claude Code CLI prompt
#   INSTALL_CODEX=yes|no             Codex CLI prompt
#   REPO_URL=git@github.com:…        Use SSH clone URL (for private repos)
#   REPO_URL=https://TOKEN@github…   Use HTTPS URL with PAT baked in
#   SKIP_CONFIRM=1                   Skip the "ready to install?" gate
#
# Defaults:
#   APP_USER=ahs
#   INSTALL_DIR=/opt/yupp-agent
#   PYTHON_VERSION=3.12

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/yupp-ai/yupp-agent.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/yupp-agent}"
APP_USER="${APP_USER:-ahs}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
# Pin Poetry to match local dev machines. Poetry 1.x and 2.x compute lock-file
# content-hashes differently, so mixing versions makes `poetry install` complain
# about "pyproject.toml changed significantly since poetry.lock was last generated"
# even when the lock is correct.
POETRY_VERSION="${POETRY_VERSION:-1.8.5}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()  { echo -e "${GREEN}[install]${NC} $*"; }
warn()  { echo -e "${YELLOW}[install]${NC} $*"; }
error() { echo -e "${RED}[install]${NC} $*" >&2; exit 1; }

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

# prompt_yes_no "Question" [default-y-or-n] → returns 0 for yes, 1 for no.
# Reads from /dev/tty so `curl | sudo bash` still gets interactive input.
prompt_yes_no() {
    local prompt="$1" default="${2:-y}"
    local hint="[Y/n]"; [[ "$default" == "n" ]] && hint="[y/N]"
    local answer
    read -rp "$prompt $hint " answer </dev/tty
    answer="${answer:-$default}"
    [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}

prompt_enter() {
    local msg="${1:-Press Enter to continue…}"
    read -rp "$msg " _ </dev/tty
}

# Parse a GitHub URL (SSH or HTTPS, with or without .git, with or without
# userinfo like "oauth2:TOKEN@") into "owner/repo". Prints nothing if it
# can't recognize the URL — callers must handle empty output.
repo_slug_from_url() {
    local url="$1"
    if [[ "$url" =~ ^git@github\.com:([^/]+/[^/.]+)(\.git)?/?$ ]]; then
        echo "${BASH_REMATCH[1]}"
    elif [[ "$url" =~ ^https?://([^@]+@)?[^/]*github\.com/([^/]+/[^/.]+)(\.git)?/?$ ]]; then
        echo "${BASH_REMATCH[2]}"
    fi
}

[[ $EUID -ne 0 ]] && error "Run this script as root (or with sudo)."

TOTAL_STEPS=8

# ---------------------------------------------------------------------------
# Plan + confirm
# ---------------------------------------------------------------------------

banner "AHS Monolith Installer"

cat <<EOF
This script will set up a single-VM AHS deployment on this box.

Target OS:      $(lsb_release -ds 2>/dev/null || cat /etc/os-release | head -1 | cut -d= -f2-)
Install dir:    ${INSTALL_DIR}
System user:    ${APP_USER} (created if missing)
Python:         ${PYTHON_VERSION} (via deadsnakes PPA if not present)
Repo:           ${REPO_URL}

It will do ${TOTAL_STEPS} steps:

  1. Install system packages   — Python ${PYTHON_VERSION}, PostgreSQL 16, Redis 7, Poetry
  2. Create system user        — '${APP_USER}' and ${INSTALL_DIR}
  3. Clone the repo            — walks you through SSH deploy key setup if needed
  4. Install Python deps       — poetry install (production, no dev extras)
  5. Install systemd units     — yupp-agent, yupp-streamlit (enabled, not started)
  6. Create data directories   — for logs, cache, etc.
  7. Install agent CLIs        — Claude Code + Codex (optional, prompted)
  8. Postgres DB + roles       — 'yadb' + schema_manager (DDL) + be_app_user (runtime)
                                 passwords saved to /opt/yupp-agent/.pg-creds

After that, you'll still need to:

  a. Run the setup wizard:
       sudo -u ${APP_USER} bash -c 'cd ${INSTALL_DIR} && .venv/bin/python -m ypl.mono_server.setup'
  b. Add LLM API keys to ${INSTALL_DIR}/.env
  c. Authenticate the agent CLIs (claude login, codex login)
  d. Start the services:     sudo systemctl start ahs-mono ahs-streamlit

The script is safe to re-run — it skips steps that are already done.

EOF

if [[ -z "${SKIP_CONFIRM:-}" ]]; then
    if ! prompt_yes_no "Ready to install?"; then
        info "Aborted. Nothing changed."
        exit 0
    fi
fi

# ---------------------------------------------------------------------------
# Step 1. System packages
# ---------------------------------------------------------------------------
step 1 "$TOTAL_STEPS" "System packages"

info "Updating apt package index…"
apt-get update -q

info "Installing base system dependencies…"
apt-get install -y --no-install-recommends \
    curl git ca-certificates gnupg lsb-release \
    build-essential cmake g++ make \
    libpq-dev libssl-dev libffi-dev \
    software-properties-common apt-transport-https

if ! command -v "python${PYTHON_VERSION}" &>/dev/null; then
    info "Installing Python ${PYTHON_VERSION} via deadsnakes PPA…"
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -q
    apt-get install -y --no-install-recommends \
        "python${PYTHON_VERSION}" "python${PYTHON_VERSION}-venv" "python${PYTHON_VERSION}-dev"
fi
info "Python: $(python${PYTHON_VERSION} --version)"

if ! command -v psql &>/dev/null; then
    info "Installing PostgreSQL 16 (with pgvector)…"
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /usr/share/keyrings/postgresql.gpg
    echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] \
https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list
    apt-get update -q
    apt-get install -y --no-install-recommends postgresql-16 postgresql-client-16 postgresql-16-pgvector
fi
systemctl enable --now postgresql
info "PostgreSQL: $(psql --version)"

if ! command -v redis-server &>/dev/null; then
    info "Installing Redis 7…"
    curl -fsSL https://packages.redis.io/gpg \
        | gpg --dearmor -o /usr/share/keyrings/redis.gpg
    echo "deb [signed-by=/usr/share/keyrings/redis.gpg] \
https://packages.redis.io/deb $(lsb_release -cs) main" \
        > /etc/apt/sources.list.d/redis.list
    apt-get update -q
    apt-get install -y --no-install-recommends redis-server
fi
systemctl enable --now redis-server
info "Redis: $(redis-server --version | head -1)"

INSTALLED_POETRY_VERSION="$(poetry --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)"
if [[ -z "$INSTALLED_POETRY_VERSION" ]]; then
    info "Installing Poetry ${POETRY_VERSION}…"
    curl -sSL https://install.python-poetry.org | POETRY_HOME=/usr/local POETRY_VERSION="$POETRY_VERSION" python3 -
elif [[ "$INSTALLED_POETRY_VERSION" != "$POETRY_VERSION" ]]; then
    warn "Existing Poetry is ${INSTALLED_POETRY_VERSION}, expected ${POETRY_VERSION}."
    warn "Reinstalling Poetry ${POETRY_VERSION} to avoid lock-file hash mismatches."
    curl -sSL https://install.python-poetry.org | POETRY_HOME=/usr/local POETRY_VERSION="$POETRY_VERSION" python3 - --force
fi
info "Poetry: $(poetry --version)"

# ---------------------------------------------------------------------------
# Step 2. App user + directory
# ---------------------------------------------------------------------------
step 2 "$TOTAL_STEPS" "App user + install directory"

if ! id "$APP_USER" &>/dev/null; then
    info "Creating system user '${APP_USER}' with home=${INSTALL_DIR}…"
    useradd --system --no-create-home --shell /bin/bash \
            --home-dir "$INSTALL_DIR" "$APP_USER"
else
    info "System user '${APP_USER}' already exists."
fi

if [[ ! -d "$INSTALL_DIR" ]]; then
    info "Creating ${INSTALL_DIR}…"
    mkdir -p "$INSTALL_DIR"
fi
chown -R "${APP_USER}:${APP_USER}" "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# Step 3. Clone / update repo (with guided deploy-key setup if needed)
# ---------------------------------------------------------------------------
step 3 "$TOTAL_STEPS" "Clone or update the repo"

# Pre-seed ~/.ssh and github.com in known_hosts so future SSH pulls don't prompt.
sudo -u "$APP_USER" mkdir -p "${INSTALL_DIR}/.ssh"
sudo -u "$APP_USER" chmod 700 "${INSTALL_DIR}/.ssh"
if ! sudo -u "$APP_USER" grep -q "github.com" "${INSTALL_DIR}/.ssh/known_hosts" 2>/dev/null; then
    info "Pinning github.com's SSH host key (ssh-keyscan)…"
    sudo -u "$APP_USER" bash -c "ssh-keyscan -H github.com >> '${INSTALL_DIR}/.ssh/known_hosts' 2>/dev/null"
    sudo -u "$APP_USER" chmod 644 "${INSTALL_DIR}/.ssh/known_hosts"
fi

clone_attempt() {
    # Probe the remote without side effects. `git ls-remote HEAD` exits non-zero
    # on auth failure / missing repo. GIT_TERMINAL_PROMPT=0 + GIT_ASKPASS=/bin/true
    # prevents git from interactively prompting for HTTPS creds (we want the
    # probe to silently fail, not hang asking for a username/password).
    # BatchMode=yes does the same for SSH URLs — fail instead of prompt.
    sudo -u "$APP_USER" env \
        GIT_TERMINAL_PROMPT=0 \
        GIT_ASKPASS=/bin/true \
        GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5" \
        git ls-remote "$REPO_URL" HEAD &>/dev/null
}

clone_into_nonempty_dir() {
    # `git clone` refuses non-empty targets, but by the time we get here the
    # install dir may already contain .ssh/ from the deploy-key walkthrough
    # (or an aborted earlier run). Clone into a tmp dir and move contents in,
    # skipping anything that would collide.
    local tmpclone
    tmpclone=$(sudo -u "$APP_USER" mktemp -d)
    sudo -u "$APP_USER" git clone "$REPO_URL" "$tmpclone"
    info "Moving clone contents into ${INSTALL_DIR}…"
    sudo -u "$APP_USER" env TMPCLONE="$tmpclone" DEST="$INSTALL_DIR" bash <<'EOSH'
set -e
shopt -s dotglob nullglob
for f in "$TMPCLONE"/*; do
    base=$(basename "$f")
    target="$DEST/$base"
    if [[ -e "$target" ]]; then
        echo "  skipping (already exists): $base"
    else
        mv "$f" "$DEST/"
    fi
done
EOSH
    rmdir "$tmpclone" 2>/dev/null || rm -rf "$tmpclone"
}

guide_deploy_key_setup() {
    local key_path="${INSTALL_DIR}/.ssh/id_ed25519"
    local slug; slug=$(repo_slug_from_url "$REPO_URL")

    echo
    echo -e "${BOLD}${YELLOW}The repo at ${REPO_URL} is not publicly accessible.${NC}"
    echo
    echo "We'll set up an SSH deploy key so this VM can pull the repo."
    echo "Deploy keys are:"
    echo "   • Per-VM, per-repo"
    echo "   • Read-only"
    echo "   • Long-lived (no expiration, no rotation)"
    echo

    if [[ ! -f "$key_path" ]]; then
        info "Generating a fresh SSH key in ${key_path}…"
        sudo -u "$APP_USER" ssh-keygen -t ed25519 -N '' \
            -f "$key_path" -C "${APP_USER}@$(hostname)" >/dev/null
    else
        info "Reusing existing SSH key at ${key_path}."
    fi

    echo
    echo -e "${BOLD}STEP A — Copy this public key (between the lines):${NC}"
    echo
    echo "────────────────────────8<────────────────────────"
    sudo -u "$APP_USER" cat "${key_path}.pub"
    echo "────────────────────────8<────────────────────────"
    echo
    if [[ -n "$slug" ]]; then
        echo -e "${BOLD}STEP B — Open this URL in your browser:${NC}"
        echo
        echo "    https://github.com/${slug}/settings/keys/new"
        echo
    else
        echo -e "${BOLD}STEP B — Open the repo's deploy-keys page on GitHub:${NC}"
        echo
        echo "    GitHub → your repo → Settings → Deploy keys → Add deploy key"
        echo
    fi
    echo -e "${BOLD}STEP C — Fill in the form:${NC}"
    echo
    echo "    Title:              vm-$(hostname)"
    echo "    Key:                (paste the key from STEP A)"
    echo "    Allow write access: UNCHECKED"
    echo
    echo "    Click 'Add key'."
    echo

    prompt_enter "Press Enter here once the deploy key is added on GitHub…"

    # Switch the URL to SSH form so the deploy key is actually used.
    if [[ "$REPO_URL" != git@* ]]; then
        if [[ -n "$slug" ]]; then
            info "Switching REPO_URL from HTTPS to SSH (git@github.com:${slug}.git) so the deploy key is used."
            REPO_URL="git@github.com:${slug}.git"
        fi
    fi

    info "Verifying access to ${REPO_URL}…"
    if ! clone_attempt; then
        error "Still can't reach ${REPO_URL}. Check that the deploy key is added at https://github.com/${slug}/settings/keys and re-run this script."
    fi
    info "Deploy key works ✓"
}

if [[ -d "${INSTALL_DIR}/.git" ]]; then
    info "Repo already cloned at ${INSTALL_DIR}. Pulling latest…"
    sudo -u "$APP_USER" git -C "$INSTALL_DIR" pull --ff-only \
        || error "git pull failed. Fix the issue (e.g. deploy key missing) and re-run."
else
    info "Testing access to ${REPO_URL}… (silent probe — no credential prompts)"
    if ! clone_attempt; then
        guide_deploy_key_setup
    fi
    info "Cloning ${REPO_URL} → ${INSTALL_DIR}…"
    clone_into_nonempty_dir
fi

# ---------------------------------------------------------------------------
# Step 4. Python dependencies
# ---------------------------------------------------------------------------
step 4 "$TOTAL_STEPS" "Python dependencies (poetry install)"

info "This takes a few minutes on a fresh box (numpy/psycopg wheels)…"
sudo -u "$APP_USER" env INSTALL_DIR="$INSTALL_DIR" PYTHON_VERSION="$PYTHON_VERSION" bash -c '
    cd "$INSTALL_DIR"
    poetry env use "python${PYTHON_VERSION}"
    poetry install --no-root --without dev --compile
'

# Resolve the venv path AFTER first nuking any stale `.venv` symlink. Otherwise
# `poetry env info --path` can resolve through a previous-run's `.venv` symlink
# and return ${INSTALL_DIR}/.venv itself — then ln -sfn creates a self-loop
# and you get "Too many levels of symbolic links" on every later invocation.
VENV_LINK="${INSTALL_DIR}/.venv"
if [[ -L "$VENV_LINK" ]]; then
    # Dereference once; if it points at itself, delete it before asking poetry.
    link_target=$(readlink -f "$VENV_LINK" 2>/dev/null || true)
    if [[ -z "$link_target" || "$link_target" == "$VENV_LINK" ]]; then
        warn "Stale / self-referential .venv symlink at ${VENV_LINK} — removing."
        rm -f "$VENV_LINK"
    fi
fi
VENV_DIR=$(sudo -u "$APP_USER" env INSTALL_DIR="$INSTALL_DIR" bash -c 'cd "$INSTALL_DIR" && poetry env info --path')
if [[ -z "$VENV_DIR" || "$VENV_DIR" == "$VENV_LINK" ]]; then
    error "poetry env info --path returned an unusable value: '${VENV_DIR}'. Check that step 4 (poetry install) actually succeeded."
fi
sudo -u "$APP_USER" ln -sfn "$VENV_DIR" "$VENV_LINK"
info "Virtual environment: ${VENV_DIR}"
info "  symlinked: ${VENV_LINK} -> ${VENV_DIR}"

# ---------------------------------------------------------------------------
# Step 5. systemd units
# ---------------------------------------------------------------------------
step 5 "$TOTAL_STEPS" "systemd service units"

info "Installing ahs-mono.service and ahs-streamlit.service…"
cp "${INSTALL_DIR}/deploy/systemd/ahs-mono.service"     /etc/systemd/system/
cp "${INSTALL_DIR}/deploy/systemd/ahs-streamlit.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable ahs-mono ahs-streamlit
info "Units enabled (will auto-start on boot). Not started yet — need setup wizard first."

# ---------------------------------------------------------------------------
# Step 6. Data / log / cache directories
# ---------------------------------------------------------------------------
step 6 "$TOTAL_STEPS" "Runtime directories"

mkdir -p /var/log/ahs-mono
chown "${APP_USER}:${APP_USER}" /var/log/ahs-mono
mkdir -p "${INSTALL_DIR}/data" "${INSTALL_DIR}/.cache"
chown "${APP_USER}:${APP_USER}" "${INSTALL_DIR}/data" "${INSTALL_DIR}/.cache"
info "Created /var/log/ahs-mono, ${INSTALL_DIR}/data, ${INSTALL_DIR}/.cache"

# ---------------------------------------------------------------------------
# Step 7. Agent executor CLIs (optional)
# ---------------------------------------------------------------------------
step 7 "$TOTAL_STEPS" "Agent executor CLIs (optional)"

echo "AHS spawns Claude Code and/or Codex as subprocesses when running agents."
echo "At least one is needed for agents to actually do work."
echo "(You can skip both now and install them later.)"
echo

# --- Claude Code CLI -------------------------------------------------------
CLAUDE_CHOICE="${INSTALL_CLAUDE_CODE:-}"
if [[ -z "$CLAUDE_CHOICE" ]]; then
    if prompt_yes_no "Install Claude Code CLI as ${APP_USER}?"; then
        CLAUDE_CHOICE=yes
    else
        CLAUDE_CHOICE=no
    fi
fi
if [[ "$CLAUDE_CHOICE" == "yes" ]]; then
    info "Installing Claude Code CLI as ${APP_USER}…"
    sudo -u "$APP_USER" bash -c 'curl -fsSL https://claude.ai/install.sh | bash' \
        || warn "Claude Code install failed — continuing. Retry manually later."
    # The Claude installer drops the binary into $HOME/.local/bin but doesn't
    # add it to PATH. Append an idempotent PATH export to ~/.bashrc so
    # `sudo -iu $APP_USER claude …` just works.
    BASHRC="${INSTALL_DIR}/.bashrc"
    if ! sudo -u "$APP_USER" test -f "$BASHRC" || \
       ! sudo -u "$APP_USER" grep -qF '.local/bin' "$BASHRC" 2>/dev/null; then
        info "Adding ${APP_USER}'s .local/bin to its shell PATH (${BASHRC})…"
        sudo -u "$APP_USER" bash -c "echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> '$BASHRC'"
    fi
else
    info "Skipping Claude Code CLI."
fi

# --- Codex CLI (requires Node.js) ------------------------------------------
CODEX_CHOICE="${INSTALL_CODEX:-}"
if [[ -z "$CODEX_CHOICE" ]]; then
    if prompt_yes_no "Install Codex CLI (installs Node.js 20 via NodeSource if missing)?"; then
        CODEX_CHOICE=yes
    else
        CODEX_CHOICE=no
    fi
fi
if [[ "$CODEX_CHOICE" == "yes" ]]; then
    if ! command -v node &>/dev/null; then
        info "Installing Node.js 20 via NodeSource…"
        curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
        apt-get install -y --no-install-recommends nodejs
    fi
    info "Installing Codex CLI globally via npm…"
    npm install -g @openai/codex \
        || warn "Codex install failed — continuing. Retry manually later."
else
    info "Skipping Codex CLI."
fi

# ---------------------------------------------------------------------------
# Step 8. Postgres database + app roles
# ---------------------------------------------------------------------------
step 8 "$TOTAL_STEPS" "PostgreSQL database + app roles"

# Database name matches the convention used everywhere else in the codebase
# (dump_staging_to_local.py, alembic tests, prod). Overridable for non-default
# setups.
DB_NAME="${DB_NAME:-yadb}"
CREDS_FILE="${INSTALL_DIR}/.pg-creds"

# Two roles, least-privilege pattern matching prod:
#   schema_manager — DDL / Alembic / initial setup. Owns yadb + public schema.
#   be_app_user    — runtime. CRUD only, no DDL. What AHS / MCP / SAG use.
#
# Passwords are random per-role. Reuse existing passwords if .pg-creds already
# exists — re-running install.sh must not rotate the running service out from
# under itself.
if sudo test -r "$CREDS_FILE"; then
    info "Reusing Postgres role passwords from ${CREDS_FILE} (per your preference)."
    SCHEMA_MANAGER_PASSWORD=$(sudo awk -F= '/^SCHEMA_MANAGER_PASSWORD=/{print $2}' "$CREDS_FILE")
    BE_APP_USER_PASSWORD=$(sudo awk -F= '/^BE_APP_USER_PASSWORD=/{print $2}' "$CREDS_FILE")
    if [[ -z "$SCHEMA_MANAGER_PASSWORD" || -z "$BE_APP_USER_PASSWORD" ]]; then
        error "${CREDS_FILE} is malformed (missing SCHEMA_MANAGER_PASSWORD or BE_APP_USER_PASSWORD). Delete it and re-run to regenerate."
    fi
else
    info "Generating fresh random passwords for schema_manager + be_app_user…"
    SCHEMA_MANAGER_PASSWORD=$(openssl rand -hex 16)
    BE_APP_USER_PASSWORD=$(openssl rand -hex 16)
fi

# Create DB if missing.
if sudo -u postgres psql -lqt | cut -d\| -f1 | tr -d ' ' | grep -qx "$DB_NAME"; then
    info "PostgreSQL database '${DB_NAME}' already exists."
else
    info "Creating PostgreSQL database '${DB_NAME}'…"
    sudo -u postgres createdb "$DB_NAME"
fi

# Create / update roles (idempotent).
#
# psql :'varname' substitution doesn't reach inside DO $$…$$ blocks (the
# dollar-quoting is opaque to psql's variable expansion), so we interpolate
# the passwords at the bash level and rely on server-side `format(%L)` for
# safe SQL literal quoting. Passwords are hex from `openssl rand -hex 16`
# — no special SQL chars, but %L is still the right tool for the job.
info "Configuring Postgres roles schema_manager + be_app_user…"
sudo -u postgres psql -v ON_ERROR_STOP=1 >/dev/null <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'schema_manager') THEN
        EXECUTE format('CREATE ROLE schema_manager LOGIN PASSWORD %L CREATEDB', '${SCHEMA_MANAGER_PASSWORD}');
    ELSE
        EXECUTE format('ALTER ROLE schema_manager WITH LOGIN PASSWORD %L CREATEDB', '${SCHEMA_MANAGER_PASSWORD}');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'be_app_user') THEN
        EXECUTE format('CREATE ROLE be_app_user LOGIN PASSWORD %L', '${BE_APP_USER_PASSWORD}');
    ELSE
        EXECUTE format('ALTER ROLE be_app_user WITH LOGIN PASSWORD %L', '${BE_APP_USER_PASSWORD}');
    END IF;
END
\$\$;

-- schema_manager owns the database (so it has free reign on DDL).
ALTER DATABASE "${DB_NAME}" OWNER TO schema_manager;
SQL

# Per-database grants + extensions: must be run inside yadb itself.
# Bash-interpolated so we can emit a literal db name into GRANT CONNECT ON
# DATABASE (which won't accept current_database() or function calls there).
info "Configuring '${DB_NAME}': install pgvector, public-schema ownership, default privs for be_app_user…"
sudo -u postgres psql -v ON_ERROR_STOP=1 -d "$DB_NAME" >/dev/null <<SQL
-- pgvector must be created by a superuser (which schema_manager isn't — and
-- shouldn't be). The baseline Alembic migration runs
-- 'CREATE EXTENSION IF NOT EXISTS vector', which becomes a no-op once this
-- has been done here as the postgres superuser.
CREATE EXTENSION IF NOT EXISTS vector;

-- schema_manager owns the public schema so Alembic can CREATE TABLE freely.
ALTER SCHEMA public OWNER TO schema_manager;

-- be_app_user: connect + read/write existing and future objects.
GRANT CONNECT ON DATABASE "${DB_NAME}" TO be_app_user;
GRANT USAGE ON SCHEMA public TO be_app_user;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO be_app_user;
GRANT USAGE, SELECT, UPDATE            ON ALL SEQUENCES IN SCHEMA public TO be_app_user;
GRANT EXECUTE                           ON ALL FUNCTIONS IN SCHEMA public TO be_app_user;

-- Default privileges — apply to objects created by schema_manager in the
-- future (which is what Alembic will do). This is the magic that keeps
-- runtime working after every migration without re-running GRANT.
ALTER DEFAULT PRIVILEGES FOR ROLE schema_manager IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES    TO be_app_user;
ALTER DEFAULT PRIVILEGES FOR ROLE schema_manager IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE            ON SEQUENCES TO be_app_user;
ALTER DEFAULT PRIVILEGES FOR ROLE schema_manager IN SCHEMA public
    GRANT EXECUTE                          ON FUNCTIONS TO be_app_user;
SQL

# Write the creds file. Owned by $APP_USER (mode 0600) — the setup wizard
# reads it as that user; nothing else on the box should touch it.
sudo tee "$CREDS_FILE" > /dev/null <<EOF
# Postgres role passwords for the AHS monolith (written by install.sh).
# The setup wizard reads this file automatically — no manual paste needed.
#
# For CI / GitHub Actions: copy SCHEMA_MANAGER_PASSWORD into a repo secret
# and build POSTGRES_CONNECTION_AGENTDB_ADMIN from it, e.g.
#   {"user":"schema_manager","password":"<secret>","host":"<dbhost>:5432","database":"${DB_NAME}"}
#
# Re-running install.sh reuses these values; to rotate, delete this file
# first and let install.sh generate fresh passwords.
DB_NAME=${DB_NAME}
SCHEMA_MANAGER_PASSWORD=${SCHEMA_MANAGER_PASSWORD}
BE_APP_USER_PASSWORD=${BE_APP_USER_PASSWORD}
EOF
sudo chown "${APP_USER}:${APP_USER}" "$CREDS_FILE"
sudo chmod 600 "$CREDS_FILE"
info "Role creds written to ${CREDS_FILE} (mode 0600, owner ${APP_USER})."

# ---------------------------------------------------------------------------
# Done — comprehensive next-steps checklist
# ---------------------------------------------------------------------------

# Pre-expand ANSI escapes into variables — heredoc bodies don't interpret
# \033 literals, so we interpolate these in instead.
B=$'\033[1m'   # bold
D=$'\033[2m'   # dim
N=$'\033[0m'   # reset

banner "✅ Installation complete!"

cat <<EOF
Everything below is now installed and ready:

  ✓ System packages  — Python ${PYTHON_VERSION}, PostgreSQL 16, Redis 7, Poetry ${POETRY_VERSION}
  ✓ System user      — ${APP_USER} (home: ${INSTALL_DIR})
  ✓ Repo             — ${INSTALL_DIR}
  ✓ Python venv      — ${VENV_DIR}
  ✓ systemd units    — yupp-agent, yupp-streamlit (enabled, not started)
  ✓ Runtime dirs     — /var/log/ahs-mono, ${INSTALL_DIR}/data, ${INSTALL_DIR}/.cache
  ✓ Database         — PostgreSQL '${DB_NAME}' (empty — Alembic migrations run in setup wizard)
  ✓ Postgres roles   — schema_manager (DDL/Alembic), be_app_user (runtime)
                       creds → /opt/yupp-agent/.pg-creds (mode 0600, owned by ${APP_USER})
$( [[ "$CLAUDE_CHOICE" == "yes" ]] && echo "  ✓ Agent CLI        — Claude Code (needs 'claude login')" || echo "  ✗ Agent CLI        — Claude Code (skipped)" )
$( [[ "$CODEX_CHOICE"  == "yes" ]] && echo "  ✓ Agent CLI        — Codex (needs 'codex login')"       || echo "  ✗ Agent CLI        — Codex (skipped)" )


${B}Services that will run on this box once started:${N}

  ${D}Service         Port   Bound to         Purpose${N}
  ahs-mono        8090   0.0.0.0          AHS + MCP + Slack/GitHub gateways (HTTP API)
  ahs-streamlit   8501   0.0.0.0          Operational dashboards (UI)
  postgresql      5432   localhost        Agent DB (${DB_NAME})
  redis           6379   localhost        Session state / pub-sub

  Internal-only by default. Don't open 5432 or 6379 to the internet.
  Only 8090 (AHS API) needs to be publicly reachable, and only if you wire up
  Slack webhooks or external MCP clients — use a Cloudflare Tunnel or a
  reverse proxy with TLS (see step 7 below).


${B}What's still left for you to do — in this order:${N}
─────────────────────────────────────────────────────────────────

 1. ${B}Run the interactive setup wizard.${N}
    Auto-detects ${INSTALL_DIR}/.pg-creds and skips password prompts.
    Asks for admin email, auto-generates secrets, writes ${INSTALL_DIR}/.env
    (mode 0600) with both runtime (be_app_user) and admin (schema_manager)
    connection strings, runs Alembic migrations as schema_manager, seeds
    roles and your admin user.

      sudo -u ${APP_USER} bash -c 'cd ${INSTALL_DIR} && .venv/bin/python -m ypl.mono_server.setup'

 2. ${B}Add at least one LLM provider API key to the .env.${N}
    At least one of ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY.

      sudo -u ${APP_USER} nano ${INSTALL_DIR}/.env

EOF

n=3
if [[ "$CLAUDE_CHOICE" == "yes" ]]; then
cat <<EOF
 ${n}. ${B}Authenticate Claude Code.${N}
    Opens a URL; paste into your browser, complete OAuth, paste code back.

      sudo -iu ${APP_USER} claude login

EOF
n=$((n+1))
fi
if [[ "$CODEX_CHOICE" == "yes" ]]; then
cat <<EOF
 ${n}. ${B}Authenticate Codex.${N}

      sudo -iu ${APP_USER} codex login

EOF
n=$((n+1))
fi
cat <<EOF
 ${n}. ${B}Start the services.${N}

      sudo systemctl start ahs-mono ahs-streamlit
      sudo systemctl status ahs-mono ahs-streamlit

EOF
n=$((n+1))
cat <<EOF
 ${n}. ${B}Verify.${N}

      curl http://localhost:8090/health
      # → {"status":"ok"}

EOF
n=$((n+1))
cat <<EOF
 ${n}. ${B}Expose to the internet (optional — required for Slack webhooks).${N}
    Cloudflare Tunnel template:

      ${INSTALL_DIR}/deploy/cloudflared/config.yml

    See ${INSTALL_DIR}/DEPLOYMENT.md for the full walkthrough.


${B}Live logs:${N}

      journalctl -u ahs-mono     -f
      journalctl -u ahs-streamlit -f

Re-run this script anytime — it's idempotent and safe to upgrade/reconfigure.

EOF
