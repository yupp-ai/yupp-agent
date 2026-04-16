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

TOTAL_STEPS=7

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

After that, you'll still need to:

  a. Run the setup wizard:   sudo -u ${APP_USER} python -m ypl.mono_server.setup
  b. Add LLM API keys to ${INSTALL_DIR}/.env
  c. Authenticate the agent CLIs (claude login, codex login)
  d. Start the services:     sudo systemctl start yupp-agent yupp-streamlit

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

if ! command -v poetry &>/dev/null; then
    info "Installing Poetry…"
    curl -sSL https://install.python-poetry.org | POETRY_HOME=/usr/local python3 -
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
    # Probe the remote without side effects — `git ls-remote HEAD` exits non-zero
    # on auth failure / missing repo but doesn't leave a half-cloned dir behind.
    sudo -u "$APP_USER" git ls-remote "$REPO_URL" HEAD &>/dev/null
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
    info "Testing access to ${REPO_URL}…"
    if ! clone_attempt; then
        guide_deploy_key_setup
    fi
    info "Cloning ${REPO_URL} → ${INSTALL_DIR}…"
    sudo -u "$APP_USER" git clone "$REPO_URL" "$INSTALL_DIR"
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

VENV_DIR=$(sudo -u "$APP_USER" env INSTALL_DIR="$INSTALL_DIR" bash -c 'cd "$INSTALL_DIR" && poetry env info --path')
ln -sfn "$VENV_DIR" "${INSTALL_DIR}/.venv"
info "Virtual environment: ${VENV_DIR}"

# ---------------------------------------------------------------------------
# Step 5. systemd units
# ---------------------------------------------------------------------------
step 5 "$TOTAL_STEPS" "systemd service units"

info "Installing yupp-agent.service and yupp-streamlit.service…"
cp "${INSTALL_DIR}/deploy/systemd/yupp-agent.service"     /etc/systemd/system/
cp "${INSTALL_DIR}/deploy/systemd/yupp-streamlit.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable yupp-agent yupp-streamlit
info "Units enabled (will auto-start on boot). Not started yet — need setup wizard first."

# ---------------------------------------------------------------------------
# Step 6. Data / log / cache directories
# ---------------------------------------------------------------------------
step 6 "$TOTAL_STEPS" "Runtime directories"

mkdir -p /var/log/yupp-agent
chown "${APP_USER}:${APP_USER}" /var/log/yupp-agent
mkdir -p "${INSTALL_DIR}/data" "${INSTALL_DIR}/.cache"
chown "${APP_USER}:${APP_USER}" "${INSTALL_DIR}/data" "${INSTALL_DIR}/.cache"
info "Created /var/log/yupp-agent, ${INSTALL_DIR}/data, ${INSTALL_DIR}/.cache"

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
# Done — comprehensive next-steps checklist
# ---------------------------------------------------------------------------
banner "✅ Installation complete!"

cat <<EOF
Everything below is now installed:

  ✓ System packages (Python ${PYTHON_VERSION}, PostgreSQL 16, Redis 7, Poetry)
  ✓ System user ${APP_USER} at ${INSTALL_DIR}
  ✓ Repo cloned at ${INSTALL_DIR}
  ✓ Python deps in ${VENV_DIR}
  ✓ systemd units (yupp-agent, yupp-streamlit) — enabled but not started
  ✓ Log dir /var/log/yupp-agent, data/ and .cache/ under ${INSTALL_DIR}
$( [[ "$CLAUDE_CHOICE" == "yes" ]] && echo "  ✓ Claude Code CLI" || echo "  ✗ Claude Code CLI (skipped)" )
$( [[ "$CODEX_CHOICE"  == "yes" ]] && echo "  ✓ Codex CLI"       || echo "  ✗ Codex CLI (skipped)" )

What's still left to do — in this order:
─────────────────────────────────────────────────────────────────

 1. ${BOLD}Create the database.${NC}

      sudo -u postgres createdb yupp_agent

 2. ${BOLD}Run the interactive setup wizard.${NC}
    It asks for DB creds + Redis URL, generates secrets, writes
    ${INSTALL_DIR}/.env, runs Alembic migrations, and seeds your admin user.

      sudo -u ${APP_USER} bash -c 'cd ${INSTALL_DIR} && python -m ypl.mono_server.setup'

 3. ${BOLD}Add at least one LLM provider API key to the .env.${NC}

      sudo -u ${APP_USER} nano ${INSTALL_DIR}/.env
      # fill in one of:
      #   ANTHROPIC_API_KEY=sk-ant-...
      #   OPENAI_API_KEY=sk-...
      #   GOOGLE_API_KEY=...

EOF

if [[ "$CLAUDE_CHOICE" == "yes" ]]; then
cat <<EOF
 4. ${BOLD}Log the Claude Code CLI into your Anthropic account.${NC}

      sudo -u ${APP_USER} claude login
      # Opens a URL; paste it into your browser and complete OAuth.

EOF
fi
if [[ "$CODEX_CHOICE" == "yes" ]]; then
cat <<EOF
 5. ${BOLD}Log the Codex CLI into your OpenAI account.${NC}

      sudo -u ${APP_USER} codex login

EOF
fi
cat <<EOF
 6. ${BOLD}Start the services.${NC}

      sudo systemctl start yupp-agent yupp-streamlit
      sudo systemctl status yupp-agent yupp-streamlit

 7. ${BOLD}Verify.${NC}

      curl http://localhost:8090/health
      # → {"status":"ok"}

 8. ${BOLD}Expose the box to the internet (optional, needed for Slack webhooks).${NC}
    Cloudflare Tunnel template lives at:

      ${INSTALL_DIR}/deploy/cloudflared/config.yml

    See ${INSTALL_DIR}/DEPLOYMENT.md for the full walkthrough.

Live logs:

      journalctl -u yupp-agent    -f
      journalctl -u yupp-streamlit -f

Re-run this script anytime (safe to do so) to upgrade / reconfigure.

EOF
