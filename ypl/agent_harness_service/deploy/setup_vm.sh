#!/bin/bash
#
# Agent Harness Service — VM Setup Script
#
# Run on a fresh Ubuntu/Debian VM to install all dependencies,
# create directory structure, clone repos, and configure the service.
#
# Usage:
#   sudo bash setup_vm.sh           # run all steps
#   sudo bash setup_vm.sh --from 7  # start from step 7
#
set -euo pipefail

# Parse --from argument
START_STEP=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --from)
            START_STEP="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1"
            echo "Usage: sudo bash setup_vm.sh [--from STEP_NUMBER]"
            exit 1
            ;;
    esac
done

echo ""
echo "============================================"
echo "  Agent Harness Service — VM Setup"
if [ "$START_STEP" -gt 1 ]; then
    echo "  Starting from step ${START_STEP}"
fi
echo "============================================"
echo ""

# Resolve data-layout paths. Runtime resolves AHS_REPOS_DIR from AHS_DATA_DIR
# (see common/constants.py); setup must match, otherwise we clone agent repos
# into a directory the service never reads from. If /data/ahs/.env already
# exists (re-run after first install), source it so any override there wins.
if [ -f /data/ahs/.env ]; then
    set -o allexport
    # shellcheck disable=SC1091
    source /data/ahs/.env
    set +o allexport
fi
AHS_DATA_DIR="${AHS_DATA_DIR:-/data/ahs}"
AHS_REPOS_DIR="${AHS_REPOS_DIR:-${AHS_DATA_DIR}/repos}"
AHS_AGENTS_DIR="${AHS_AGENTS_DIR:-${AHS_DATA_DIR}/agents}"
AHS_SHARED_DIR="${AHS_SHARED_DIR:-${AHS_DATA_DIR}/shared}"
AHS_SESSION_LOGS_DIR="${AHS_SESSION_LOGS_DIR:-${AHS_DATA_DIR}/session_logs}"
echo "  AHS_DATA_DIR=${AHS_DATA_DIR}"
echo "  AHS_REPOS_DIR=${AHS_REPOS_DIR}"
echo ""

# Python binary path — set in step 1, but needed by step 7.
# If skipping step 1, detect the existing source-built Python.
PYTHON_VERSION="3.12.12"
PYTHON_PREFIX="/opt/python${PYTHON_VERSION}"
PYTHON_BIN="${PYTHON_PREFIX}/bin/python3.12"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3.12"
fi

# --- Step 1: System dependencies ---
if [ "$START_STEP" -le 1 ]; then
echo "--------------------------------------------"
echo "  Step 1/13: Installing system packages"
echo "--------------------------------------------"
apt-get update
apt-get install -y \
    bubblewrap \
    build-essential \
    cron \
    curl \
    git \
    jq \
    libpq-dev \
    postgresql-client \
    python3-pip \
    software-properties-common \
    tree

# Python 3.12.12+ is required by the project. Ubuntu 24.04 ships 3.12.3,
# so we build from source if the system version is too old.
# The source-built Python is installed to /opt/python<version>/ and referenced
# by full path (not symlinked over system python, which breaks stdlib resolution).
REQUIRED_PATCH=12

if [ -x "${PYTHON_PREFIX}/bin/python3.12" ]; then
    PYTHON_BIN="${PYTHON_PREFIX}/bin/python3.12"
    echo "  Source-built Python already installed: $($PYTHON_BIN --version)"
else
    CURRENT_PATCH=$(python3.12 --version 2>/dev/null | sed -n 's/Python 3\.12\.\([0-9]*\)/\1/p' || true)
    if [ -n "$CURRENT_PATCH" ] && [ "$CURRENT_PATCH" -ge "$REQUIRED_PATCH" ]; then
        PYTHON_BIN="python3.12"
        echo "  System Python is sufficient: $(python3.12 --version)"
    else
        echo "  Python ${PYTHON_VERSION}+ required (found 3.12.${CURRENT_PATCH:-none}), building from source..."
        apt-get install -y libffi-dev libssl-dev zlib1g-dev liblzma-dev libbz2-dev libreadline-dev libsqlite3-dev libncurses5-dev
        cd /tmp
        curl -O "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tgz"
        tar xzf "Python-${PYTHON_VERSION}.tgz"
        cd "Python-${PYTHON_VERSION}"
        ./configure --prefix="$PYTHON_PREFIX" --enable-optimizations
        make -j"$(nproc)"
        make altinstall
        cd /tmp && rm -rf "Python-${PYTHON_VERSION}" "Python-${PYTHON_VERSION}.tgz"
        PYTHON_BIN="${PYTHON_PREFIX}/bin/python3.12"
        echo "  Installed: $($PYTHON_BIN --version)"
    fi
fi

# Enable unprivileged user namespaces for bubblewrap sandboxing.
# Two sysctl parameters can block bwrap — check and set both.
#
# 1. kernel.unprivileged_userns_clone (older distros): controls user namespace creation
# 2. kernel.apparmor_restrict_unprivileged_userns (Ubuntu 24.04+): AppArmor restriction
#
# Without these, bwrap fails with "setting up uid map: Permission denied".
# This is safe: we intentionally use bwrap to sandbox agents, and the ahs user
# is already a restricted service account. Modern distros (Ubuntu 24.04+, Fedora, Arch)
# enable unprivileged user namespaces by default.
echo "  Configuring kernel for bubblewrap..."
SYSCTL_FILE="/etc/sysctl.d/99-bwrap.conf"
NEEDS_RELOAD=false

if [ -f /proc/sys/kernel/unprivileged_userns_clone ]; then
    CURRENT=$(cat /proc/sys/kernel/unprivileged_userns_clone)
    if [ "$CURRENT" != "1" ]; then
        sysctl -w kernel.unprivileged_userns_clone=1
        echo "kernel.unprivileged_userns_clone=1" >> "$SYSCTL_FILE"
        NEEDS_RELOAD=true
        echo "  Set kernel.unprivileged_userns_clone=1"
    fi
fi

if [ -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]; then
    CURRENT=$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns)
    if [ "$CURRENT" != "0" ]; then
        sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
        echo "kernel.apparmor_restrict_unprivileged_userns=0" >> "$SYSCTL_FILE"
        NEEDS_RELOAD=true
        echo "  Set kernel.apparmor_restrict_unprivileged_userns=0"
    fi
fi

if [ "$NEEDS_RELOAD" = true ]; then
    sysctl --system > /dev/null 2>&1
    echo "  Persisted sysctl settings to $SYSCTL_FILE"
fi

# Verify bwrap works
if sudo -u ahs bwrap --ro-bind /usr /usr --proc /proc --dev /dev --tmpfs /tmp ls / > /dev/null 2>&1; then
    echo "  bwrap verified OK"
else
    echo "  WARNING: bwrap verification failed — agents will fall back to unsandboxed execution"
fi
fi

# --- Step 1b: Lint tools (ruff, mypy) ---
# Must land in /usr/local/bin so they are:
#   (a) in PATH for all users / the systemd service, and
#   (b) visible inside every bwrap sandbox via the /usr ro-bind.
#
# Use the system python3 (not $PYTHON_BIN which may be a source-built Python
# under /opt/...) so pip places scripts in /usr/local/bin rather than
# /opt/pythonX.Y.Z/bin, which is neither in PATH nor mounted in bwrap.
if [ "$START_STEP" -le 1 ]; then
echo ""
echo "--------------------------------------------"
echo "  Step 1b/13: Installing lint tools (ruff, mypy)"
echo "--------------------------------------------"
python3 -m pip install --break-system-packages ruff mypy
# Fail fast if the tools are not reachable on PATH — a silent miss here
# means agents will fall back to slow filesystem searches at runtime.
ruff --version
mypy --version
fi

# --- Step 2: GitHub CLI ---
if [ "$START_STEP" -le 2 ]; then
echo ""
echo "--------------------------------------------"
echo "  Step 2/13: Installing GitHub CLI"
echo "--------------------------------------------"
if ! command -v gh &> /dev/null; then
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | tee /usr/share/keyrings/githubcli-archive-keyring.gpg > /dev/null
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list
    apt-get update
    apt-get install -y gh
else
    echo "  Already installed: $(gh --version | head -1)"
fi
fi

# --- Step 3: Create service user ---
if [ "$START_STEP" -le 3 ]; then
echo ""
echo "--------------------------------------------"
echo "  Step 3/13: Creating service user"
echo "--------------------------------------------"
if ! id -u ahs &> /dev/null; then
    useradd -r -m -d /home/ahs -s /bin/bash ahs
    echo "  Created user: ahs"
else
    echo "  User ahs already exists"
fi
fi

# --- Step 4: Install Claude Code CLI + OpenAI Codex CLI ---
if [ "$START_STEP" -le 4 ]; then
echo ""
echo "--------------------------------------------"
echo "  Step 4/13: Installing Claude Code CLI + Codex CLI"
echo "--------------------------------------------"
sudo -u ahs -H bash -lc 'cd ~ && curl -fsSL https://claude.ai/install.sh | bash'

# Install Node.js (required for Codex CLI) via NodeSource
if ! command -v node &> /dev/null; then
    echo "  Installing Node.js 20.x..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
else
    echo "  Node.js already installed: $(node --version)"
fi

# Install OpenAI Codex CLI globally
echo "  Installing OpenAI Codex CLI..."
npm install -g @openai/codex
echo "  codex: $(codex --version 2>/dev/null || echo 'install failed')"
fi

# --- Step 5: Create directory structure ---
if [ "$START_STEP" -le 5 ]; then
echo ""
echo "--------------------------------------------"
echo "  Step 5/13: Creating directory structure"
echo "--------------------------------------------"
mkdir -p "${AHS_DATA_DIR}"/{shared,agents,repos,sessions,session_logs,artifacts,attachments,memories}
chown -R ahs:ahs "${AHS_DATA_DIR}"
echo "  ${AHS_DATA_DIR}/ directory structure ready"

# Seed git identity for the ahs user. The sandbox mounts ~ahs/.gitconfig
# read-only into the bwrap namespace; without [user] set here, sandboxed
# `git commit` fails with "Author identity unknown" and the worktree's
# shared .git/config is read-only so agents can't set it at commit time.
AGENT_GIT_USER_NAME="${AGENT_GIT_USER_NAME:-Yupp Agent}"
AGENT_GIT_USER_EMAIL="${AGENT_GIT_USER_EMAIL:-agent@yupp.ai}"
sudo -u ahs git config --global user.name "$AGENT_GIT_USER_NAME"
sudo -u ahs git config --global user.email "$AGENT_GIT_USER_EMAIL"
echo "  Git identity set for ahs: $AGENT_GIT_USER_NAME <$AGENT_GIT_USER_EMAIL>"
fi

# --- Step 6: Clone the service repo ---
if [ "$START_STEP" -le 6 ]; then
echo ""
echo "--------------------------------------------"
echo "  Step 6/13: Cloning yupp-agent repo"
echo "--------------------------------------------"
if [ ! -d /opt/yupp-agent/.git ]; then
    mkdir -p /opt/yupp-agent
    chown ahs:ahs /opt/yupp-agent
    sudo -u ahs git clone https://github.com/yupp-ai/yupp-agent.git /opt/yupp-agent
else
    echo "  Already cloned at /opt/yupp-agent"
fi
fi

# --- Step 7: Install Python dependencies ---
if [ "$START_STEP" -le 7 ]; then
echo ""
echo "--------------------------------------------"
echo "  Step 7/13: Installing Python dependencies"
echo "--------------------------------------------"
cd /opt/yupp-agent
# Recreate venv if missing, corrupted, or Python version changed
VENV_PY_VER=$(sudo -u ahs .venv/bin/python3 --version 2>/dev/null || echo "none")
EXPECTED_PY_VER=$($PYTHON_BIN --version 2>/dev/null || echo "none")
if [ ! -d .venv ] || [ "$VENV_PY_VER" = "none" ] || [ "$VENV_PY_VER" != "$EXPECTED_PY_VER" ]; then
    echo "  Creating venv (python: $EXPECTED_PY_VER, venv: $VENV_PY_VER)"
    sudo -u ahs rm -rf .venv
    # Use --without-pip because source-built Python may lack ensurepip
    sudo -u ahs "$PYTHON_BIN" -m venv --without-pip .venv
    sudo -u ahs bash -c 'cd /opt/yupp-agent && curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python3'
else
    echo "  Venv OK: $VENV_PY_VER"
fi
sudo -u ahs .venv/bin/pip install poetry
# Some dependencies are hosted on private GitHub repos and require a token.
# STRIPE_GITHUB_TOKEN must be set in the caller's environment.
if [ -z "${STRIPE_GITHUB_TOKEN:-}" ]; then
    echo "  WARNING: STRIPE_GITHUB_TOKEN not set — private dependencies may fail to install"
fi
sudo -u ahs STRIPE_GITHUB_TOKEN="${STRIPE_GITHUB_TOKEN:-}" bash -c '
cd /opt/yupp-agent
[ -n "$STRIPE_GITHUB_TOKEN" ] && git config --global url."https://${STRIPE_GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"
.venv/bin/poetry install --no-root
.venv/bin/poetry build
.venv/bin/pip install -e .
[ -n "$STRIPE_GITHUB_TOKEN" ] && git config --global --unset url."https://${STRIPE_GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"
'
fi

# --- Step 8: Set up environment file ---
if [ "$START_STEP" -le 8 ]; then
echo ""
echo "--------------------------------------------"
echo "  Step 8/13: Setting up environment file"
echo "--------------------------------------------"
if [ ! -f /data/ahs/.env ]; then
    cp /opt/yupp-agent/ypl/agent_harness_service/deploy/env.template /data/ahs/.env
    chown ahs:ahs /data/ahs/.env
    chmod 600 /data/ahs/.env
    echo "  Created /data/ahs/.env from template"
    echo "  !!! Edit it with your actual values (see DEPLOYMENT.md) !!!"
else
    echo "  /data/ahs/.env already exists, skipping"
fi
fi

# --- Step 9: Set up agent configs ---
if [ "$START_STEP" -le 9 ]; then
echo ""
echo "--------------------------------------------"
echo "  Step 9/13: Copying agent configs"
echo "--------------------------------------------"
if [ ! -d "${AHS_AGENTS_DIR}/sre" ]; then
    mkdir -p "${AHS_AGENTS_DIR}"
    cp -r /opt/yupp-agent/ypl/agent_harness_service/deploy/agent_configs/* "${AHS_AGENTS_DIR}/"
    chown -R ahs:ahs "${AHS_AGENTS_DIR}"
    echo "  Copied agent configs to ${AHS_AGENTS_DIR}/"
else
    echo "  Agent configs already exist, skipping"
fi
fi

# --- Step 10: Set up shared identity files ---
if [ "$START_STEP" -le 10 ]; then
echo ""
echo "--------------------------------------------"
echo "  Step 10/13: Setting up shared identity files"
echo "--------------------------------------------"
mkdir -p "${AHS_SHARED_DIR}"
cp -n /opt/yupp-agent/ypl/agent_harness_service/deploy/shared/SOUL.md "${AHS_SHARED_DIR}/" 2>/dev/null || true
cp -n /opt/yupp-agent/ypl/agent_harness_service/deploy/shared/WORKSPACE.md "${AHS_SHARED_DIR}/" 2>/dev/null || true
mkdir -p "${AHS_SHARED_DIR}/raw_executor"
cp -n /opt/yupp-agent/ypl/agent_harness_service/deploy/shared/raw_executor/RAW_EXECUTOR.md "${AHS_SHARED_DIR}/raw_executor/" 2>/dev/null || true
mkdir -p "${AHS_SHARED_DIR}/tasks"
cp -n /opt/yupp-agent/ypl/agent_harness_service/deploy/shared/tasks/TASK_EXECUTION.md "${AHS_SHARED_DIR}/tasks/" 2>/dev/null || true
chown -R ahs:ahs "${AHS_SHARED_DIR}"
echo "  Shared identity files ready in ${AHS_SHARED_DIR}/"
fi

# --- Step 11: Clone code repos for agent access ---
if [ "$START_STEP" -le 11 ]; then
echo ""
echo "--------------------------------------------"
echo "  Step 11/13: Cloning code repos for agents"
echo "--------------------------------------------"
mkdir -p "${AHS_REPOS_DIR}"
chown ahs:ahs "${AHS_REPOS_DIR}"
cd "${AHS_REPOS_DIR}"
for repo in yupp-agent; do
    if [ ! -d "$repo" ]; then
        sudo -u ahs git clone https://github.com/yupp-ai/${repo}.git "$repo"
    else
        # Heal origin if it drifted to SSH (e.g. re-cloned by hand via deploy key).
        # A read-only deploy key blocks push; HTTPS + gh credential helper works.
        cur_url=$(sudo -u ahs git -C "$repo" remote get-url origin 2>/dev/null || echo "")
        if [[ "$cur_url" == git@github.com:* ]] || [[ "$cur_url" == ssh://* ]]; then
            sudo -u ahs git -C "$repo" remote set-url origin "https://github.com/yupp-ai/${repo}.git"
            echo "  $repo: rewrote origin SSH -> HTTPS"
        else
            echo "  $repo already cloned"
        fi
    fi
done
fi

# --- Step 12: Install systemd service ---
if [ "$START_STEP" -le 12 ]; then
echo ""
echo "--------------------------------------------"
echo "  Step 12/13: Installing systemd service"
echo "--------------------------------------------"
cp /opt/yupp-agent/ypl/agent_harness_service/deploy/ahs.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable ahs
echo "  ahs.service installed and enabled"
fi

# --- Step 13: Set up cron jobs ---
if [ "$START_STEP" -le 13 ]; then
echo ""
echo "--------------------------------------------"
echo "  Step 13/13: Setting up cron jobs"
echo "--------------------------------------------"
DEPLOY_DIR="/opt/yupp-agent/ypl/agent_harness_service/deploy"
LOG_DIR="${AHS_SESSION_LOGS_DIR}"
mkdir -p "${LOG_DIR}"
chown ahs:ahs "${LOG_DIR}"

# Validate the GitHub App private key is in place. gh_app_auth.sh mints an
# installation token from this key every 50 min; without it, the HTTPS
# credential helper has no bot token and fetches/pushes over HTTPS fail.
if [ ! -f /data/ahs/github-app-key.pem ]; then
    echo ""
    echo "  !!! /data/ahs/github-app-key.pem NOT FOUND !!!"
    echo "      Copy the GitHub App private key to that path (chown ahs:ahs, chmod 600)"
    echo "      before the first cron run, or the 50-min refresh will fail silently."
fi

# Install cron jobs in root's crontab (all run as ahs user via sudo -u).
# Note: service-code + agent-repo pulls are handled by deploy-latest.sh and
# the ahs-pull-agent-repos.timer systemd unit respectively — no cron entries
# needed for those. Only the gh_app_auth token refresh runs out of cron.
({ crontab -l 2>/dev/null || true; } | grep -v -e gh_app_auth -e sync_configs -e pull_agent_repos || true
cat <<CRON
# --- Agent Harness Service cron jobs ---
# Refresh GitHub App token every 50 min (tokens expire after 1 hour)
*/50 * * * * sudo -u ahs bash ${DEPLOY_DIR}/gh_app_auth.sh >> ${LOG_DIR}/gh_auth.log 2>&1
CRON
) | crontab -
echo "  Cron jobs installed (gh_app_auth)"
fi

echo ""
echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Edit /data/ahs/.env with your actual values (see DEPLOYMENT.md)"
echo "  2. Place the GitHub App private key at /data/ahs/github-app-key.pem"
echo "     (chown ahs:ahs, chmod 600), then run:"
echo "       sudo -u ahs bash /opt/yupp-agent/ypl/agent_harness_service/deploy/gh_app_auth.sh"
echo "     This authenticates the gh CLI; the 50-min cron refresh keeps it fresh."
echo "  3. (Optional) Edit agent configs in ${AHS_AGENTS_DIR}/"
echo "  4. Start the service: sudo systemctl start ahs"
echo "  5. Check status: sudo systemctl status ahs / journalctl -u ahs -f"
echo ""
