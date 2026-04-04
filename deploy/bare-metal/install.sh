#!/usr/bin/env bash
# deploy/bare-metal/install.sh
#
# Bootstrap a bare-metal Debian/Ubuntu VM for the Yupp Agent Platform.
# Tested on: Ubuntu 22.04 LTS, Ubuntu 24.04 LTS, Debian 12 (Bookworm)
#
# Usage (run as root or with sudo):
#   curl -fsSL https://raw.githubusercontent.com/yupp-ai/yupp-agent/main/deploy/bare-metal/install.sh | sudo bash
#
# Or clone first and run locally:
#   sudo bash deploy/bare-metal/install.sh
#
# What this script does:
#   1. Installs system packages (Python 3.12, PostgreSQL 16, Redis 7, etc.)
#   2. Creates the 'yupp' system user and /opt/yupp-agent directory
#   3. Clones or updates the repo
#   4. Installs Python dependencies via poetry
#   5. Installs and enables systemd units
#   6. Prompts to run the interactive setup wizard
#
# After running this script:
#   sudo -u yupp python -m ypl.mono_server.setup   # first-time setup
#   sudo systemctl start yupp-agent yupp-streamlit

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/yupp-ai/yupp-agent.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/yupp-agent}"
APP_USER="${APP_USER:-yupp}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[install]${NC} $*"; }
warn()  { echo -e "${YELLOW}[install]${NC} $*"; }
error() { echo -e "${RED}[install]${NC} $*" >&2; exit 1; }

[[ $EUID -ne 0 ]] && error "Run this script as root (or with sudo)."

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
info "Updating apt package index…"
apt-get update -q

info "Installing system dependencies…"
apt-get install -y --no-install-recommends \
    curl git ca-certificates gnupg lsb-release \
    build-essential cmake g++ make \
    libpq-dev libssl-dev libffi-dev \
    software-properties-common apt-transport-https

# Python 3.12
if ! command -v python3.12 &>/dev/null; then
    info "Installing Python ${PYTHON_VERSION} via deadsnakes PPA…"
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -q
    apt-get install -y --no-install-recommends \
        "python${PYTHON_VERSION}" "python${PYTHON_VERSION}-venv" "python${PYTHON_VERSION}-dev"
fi
info "Python: $(python${PYTHON_VERSION} --version)"

# PostgreSQL 16
if ! command -v psql &>/dev/null; then
    info "Installing PostgreSQL 16…"
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

# Redis 7
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
info "Redis: $(redis-server --version)"

# Poetry
if ! command -v poetry &>/dev/null; then
    info "Installing Poetry…"
    curl -sSL https://install.python-poetry.org | POETRY_HOME=/usr/local python3 -
fi
info "Poetry: $(poetry --version)"

# ---------------------------------------------------------------------------
# 2. App user + directory
# ---------------------------------------------------------------------------
if ! id "$APP_USER" &>/dev/null; then
    info "Creating system user '${APP_USER}'…"
    useradd --system --no-create-home --shell /bin/bash \
            --home-dir "$INSTALL_DIR" "$APP_USER"
fi

if [[ ! -d "$INSTALL_DIR" ]]; then
    info "Creating ${INSTALL_DIR}…"
    mkdir -p "$INSTALL_DIR"
fi
chown -R "${APP_USER}:${APP_USER}" "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# 3. Clone / update the repo
# ---------------------------------------------------------------------------
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    info "Updating existing repo at ${INSTALL_DIR}…"
    sudo -u "$APP_USER" git -C "$INSTALL_DIR" pull --ff-only
else
    info "Cloning ${REPO_URL} → ${INSTALL_DIR}…"
    sudo -u "$APP_USER" git clone "$REPO_URL" "$INSTALL_DIR"
fi

# ---------------------------------------------------------------------------
# 4. Python venv + dependencies
# ---------------------------------------------------------------------------
info "Installing Python dependencies (this may take a few minutes)…"
sudo -u "$APP_USER" env INSTALL_DIR="$INSTALL_DIR" PYTHON_VERSION="$PYTHON_VERSION" bash -c '
    cd "$INSTALL_DIR"
    poetry env use "python${PYTHON_VERSION}"
    poetry install --no-root --without dev --compile
'

VENV_DIR=$(sudo -u "$APP_USER" env INSTALL_DIR="$INSTALL_DIR" bash -c 'cd "$INSTALL_DIR" && poetry env info --path')
ln -sfn "$VENV_DIR" "${INSTALL_DIR}/.venv"
info "Virtual environment: ${VENV_DIR}"

# ---------------------------------------------------------------------------
# 5. Systemd units
# ---------------------------------------------------------------------------
info "Installing systemd service units…"
cp "${INSTALL_DIR}/deploy/systemd/yupp-agent.service"     /etc/systemd/system/
cp "${INSTALL_DIR}/deploy/systemd/yupp-streamlit.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable yupp-agent yupp-streamlit
info "Systemd units installed and enabled (not started yet)."

# ---------------------------------------------------------------------------
# 6. Log directory
# ---------------------------------------------------------------------------
mkdir -p /var/log/yupp-agent
chown "${APP_USER}:${APP_USER}" /var/log/yupp-agent

# Data and cache directories referenced by systemd ReadWritePaths
# (ProtectSystem=strict makes the parent read-only, so create them now)
mkdir -p "${INSTALL_DIR}/data" "${INSTALL_DIR}/.cache"
chown "${APP_USER}:${APP_USER}" "${INSTALL_DIR}/data" "${INSTALL_DIR}/.cache"

# ---------------------------------------------------------------------------
# Done — prompt for setup wizard
# ---------------------------------------------------------------------------
echo
echo -e "${GREEN}========================================================${NC}"
echo -e "${GREEN}  Installation complete!${NC}"
echo -e "${GREEN}========================================================${NC}"
echo
echo "  Next step — run the interactive setup wizard:"
echo
echo "    sudo -u ${APP_USER} bash -c 'cd ${INSTALL_DIR} && python -m ypl.mono_server.setup'"
echo
echo "  Then start the services:"
echo
echo "    sudo systemctl start yupp-agent yupp-streamlit"
echo "    sudo systemctl status yupp-agent yupp-streamlit"
echo
echo "  Logs:"
echo "    journalctl -u yupp-agent   -f"
echo "    journalctl -u yupp-streamlit -f"
echo
