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

# Verify bwrap works (needs ahs user from step 3)
if command -v bwrap &> /dev/null && id -u ahs &> /dev/null; then
    if sudo -u ahs bwrap --ro-bind /usr /usr --proc /proc --dev /dev --tmpfs /tmp ls / > /dev/null 2>&1; then
        echo "  bwrap verified OK"
    else
        echo "  WARNING: bwrap verification failed — agents will fall back to unsandboxed execution"
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
mkdir -p /data/{shared,agents,repos,sessions,session_logs,memories,workspaces}
mkdir -p /data/ahs
chown -R ahs:ahs /data
echo "  /data/ directory structure ready"
fi

# --- Step 6: Clone the service repo ---
if [ "$START_STEP" -le 6 ]; then
echo ""
echo "--------------------------------------------"
echo "  Step 6/13: Cloning yupp-agent repo"
echo "--------------------------------------------"
# Private repos need a GitHub token. Pass GITHUB_TOKEN in the environment.
if [ -z "${GITHUB_TOKEN:-}" ]; then
    echo "  WARNING: GITHUB_TOKEN not set — cloning private repos will fail."
    echo "  Set it with: sudo GITHUB_TOKEN=ghp_xxx bash setup_vm.sh"
fi
CLONE_URL="https://${GITHUB_TOKEN:+${GITHUB_TOKEN}@}github.com/yupp-ai/yupp-agent.git"
if [ ! -d /opt/yupp-agent/.git ]; then
    mkdir -p /opt/yupp-agent
    chown ahs:ahs /opt/yupp-agent
    sudo -u ahs git clone "$CLONE_URL" /opt/yupp-agent
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
sudo -u ahs bash -c 'cd /opt/yupp-agent && .venv/bin/pip install poetry'
# Some dependencies are hosted on private GitHub repos and require a token.
# STRIPE_GITHUB_TOKEN must be set in the caller's environment.
if [ -z "${STRIPE_GITHUB_TOKEN:-}" ]; then
    echo "  WARNING: STRIPE_GITHUB_TOKEN not set — private dependencies may fail to install"
fi
sudo -u ahs STRIPE_GITHUB_TOKEN="${STRIPE_GITHUB_TOKEN:-}" bash -c '
cd /opt/yupp-agent
[ -n "$STRIPE_GITHUB_TOKEN" ] && git config --global url."https://${STRIPE_GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"
.venv/bin/poetry lock --no-update
.venv/bin/poetry install --no-root
.venv/bin/poetry build
.venv/bin/pip install -e .
[ -n "$STRIPE_GITHUB_TOKEN" ] && git config --global --unset url."https://${STRIPE_GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/" || true
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
if [ ! -d /data/agents/sre ]; then
    cp -r /opt/yupp-agent/ypl/agent_harness_service/deploy/agent_configs/* /data/agents/
    chown -R ahs:ahs /data/agents
    echo "  Copied agent configs to /data/agents/"
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
cp -n /opt/yupp-agent/ypl/agent_harness_service/deploy/shared/SOUL.md /data/shared/ 2>/dev/null || true
cp -n /opt/yupp-agent/ypl/agent_harness_service/deploy/shared/WORKSPACE.md /data/shared/ 2>/dev/null || true
mkdir -p /data/shared/raw_executor
cp -n /opt/yupp-agent/ypl/agent_harness_service/deploy/shared/raw_executor/RAW_EXECUTOR.md /data/shared/raw_executor/ 2>/dev/null || true
mkdir -p /data/shared/tasks
cp -n /opt/yupp-agent/ypl/agent_harness_service/deploy/shared/tasks/TASK_EXECUTION.md /data/shared/tasks/ 2>/dev/null || true
chown -R ahs:ahs /data/shared
echo "  Shared identity files ready in /data/shared/"
fi

# --- Step 11: Clone code repos for agent access ---
if [ "$START_STEP" -le 11 ]; then
echo ""
echo "--------------------------------------------"
echo "  Step 11/13: Cloning code repos for agents"
echo "--------------------------------------------"
cd /data/repos
for repo in yupp-agent yupp-mind yupp-soul yupp-head; do
    if [ ! -d "$repo" ]; then
        REPO_URL="https://${GITHUB_TOKEN:+${GITHUB_TOKEN}@}github.com/yupp-ai/${repo}.git"
        sudo -u ahs git clone "$REPO_URL" "$repo" || echo "  WARNING: Failed to clone $repo (is GITHUB_TOKEN set?)"
    else
        echo "  $repo already cloned"
    fi
done
# Symlink .claude at /data/repos/ level so Claude CLI picks up settings/hooks
ln -sfn /data/repos/yupp-agent/.claude /data/repos/.claude
echo "  /data/repos/.claude -> yupp-agent/.claude"
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
LOG_DIR="/data/session_logs"

# Install cron jobs in root's crontab (all run as ahs user via sudo -u)
({ crontab -l 2>/dev/null || true; } | grep -v -e gh_app_auth -e sync_configs -e pull_agent_repos || true
cat <<CRON
# --- Agent Harness Service cron jobs ---
# Refresh GitHub App token every 50 min (tokens expire after 1 hour)
*/50 * * * * sudo -u ahs bash /data/ahs/gh_app_auth.sh >> ${LOG_DIR}/gh_auth.log 2>&1
# Pull agent repos every 5 min (read-only checkouts in /data/repos/)
*/5 * * * * sudo -u ahs bash ${DEPLOY_DIR}/pull_agent_repos.sh >> ${LOG_DIR}/pull_agent_repos.log 2>&1
# Sync service code + configs every 30 min (git pull /opt/yupp-agent, copy to /data/)
*/30 * * * * sudo -u ahs bash ${DEPLOY_DIR}/sync_configs.sh >> ${LOG_DIR}/sync_configs.log 2>&1
CRON
) | crontab -
echo "  Cron jobs installed (gh_app_auth, pull_agent_repos, sync_configs)"
fi

echo ""
echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Edit /data/ahs/.env with your actual values (see DEPLOYMENT.md)"
echo "  2. Set up GitHub App auth:"
echo "     - Copy the GitHub App private key to /data/ahs/github-app-key.pem"
echo "     - Copy gh_app_auth.sh to /data/ahs/gh_app_auth.sh"
echo "     - chmod 600 /data/ahs/github-app-key.pem"
echo "     - chmod +x /data/ahs/gh_app_auth.sh"
echo "     - sudo -u ahs bash /data/ahs/gh_app_auth.sh"
echo "  3. (Optional) Edit agent configs in /data/agents/"
echo "  4. Start the service: sudo systemctl start ahs"
echo "  5. Check status: sudo systemctl status ahs / journalctl -u ahs -f"
echo ""
