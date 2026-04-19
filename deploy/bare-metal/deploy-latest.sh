#!/usr/bin/env bash
# deploy-latest.sh — pull latest code and restart the monolith's services.
#
# Intended to be run on the AHS monolith VM as root:
#   sudo bash /opt/yupp-agent/deploy/bare-metal/deploy-latest.sh
#
# First time? scp this file over:
#   gcloud compute scp deploy/bare-metal/deploy-latest.sh ahs-mono-prod:/tmp/deploy-latest.sh --zone=us-east5-a --project=yupp-agent
#   gcloud compute ssh ahs-mono-prod --zone=us-east5-a --project=yupp-agent --command='sudo bash /tmp/deploy-latest.sh'
#
# What it does:
#   1. git pull (fast-forward only, as the ahs user)
#   2. poetry install --no-root --without dev --compile  (cheap if nothing changed)
#   3. Copy systemd unit files to /etc/systemd/system/ (in case they changed)
#   4. alembic upgrade head (no-op if schema is already at head)
#   5. systemctl daemon-reload + restart ahs-mono ahs-streamlit
#   6. Print status

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/yupp-agent}"
APP_USER="${APP_USER:-ahs}"
SERVICES=(ahs-mono ahs-streamlit)

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[deploy]${NC} $*"; }
warn() { echo -e "${YELLOW}[deploy]${NC} $*"; }
die()  { echo -e "${YELLOW}[deploy][ERROR]${NC} $*" >&2; exit 1; }

[[ $EUID -ne 0 ]] && die "Run as root (or with sudo)."
[[ -d "${INSTALL_DIR}/.git" ]] || die "${INSTALL_DIR} is not a git checkout. Run install.sh first."

cd "$INSTALL_DIR"

# --- 1. Pull latest ---------------------------------------------------------
info "git pull (as ${APP_USER})…"
sudo -u "$APP_USER" git -C "$INSTALL_DIR" pull --ff-only \
    || die "git pull failed (deploy key missing or non-fast-forward). Fix and retry."

BEFORE_SHA=$(sudo -u "$APP_USER" git -C "$INSTALL_DIR" rev-parse HEAD)
info "Now at $(git log -1 --oneline)"

# --- 2. Sync Python deps ----------------------------------------------------
info "poetry install (cheap if nothing changed)…"
sudo -u "$APP_USER" env INSTALL_DIR="$INSTALL_DIR" bash -c '
    cd "$INSTALL_DIR"
    poetry install --no-root --without dev --compile
'

# --- 3. Sync systemd unit files --------------------------------------------
UNITS_CHANGED=0
for unit in "${SERVICES[@]}"; do
    src="${INSTALL_DIR}/deploy/systemd/${unit}.service"
    dst="/etc/systemd/system/${unit}.service"
    if [[ ! -f "$src" ]]; then
        warn "Skipping ${unit}: ${src} not found in repo."
        continue
    fi
    if ! cmp -s "$src" "$dst"; then
        info "Updating ${dst}"
        cp "$src" "$dst"
        UNITS_CHANGED=1
    fi
done

if [[ $UNITS_CHANGED -eq 1 ]]; then
    info "Unit files changed — systemctl daemon-reload"
    systemctl daemon-reload
fi

# --- 4. Database migrations ------------------------------------------------
info "alembic upgrade head (no-op if already at head)…"
sudo -u "$APP_USER" env INSTALL_DIR="$INSTALL_DIR" bash -c '
    cd "$INSTALL_DIR"
    set -a; . ./.env; set +a
    .venv/bin/python -m alembic -c alembic.ini upgrade head
' || die "alembic upgrade head failed. Fix the schema issue before retrying."

# --- 5. Restart services ---------------------------------------------------
info "Restarting: ${SERVICES[*]}"
systemctl restart "${SERVICES[@]}"

# --- 6. Status summary -----------------------------------------------------
echo
for unit in "${SERVICES[@]}"; do
    state=$(systemctl is-active "$unit" || true)
    enabled=$(systemctl is-enabled "$unit" || true)
    printf "  %-20s active=%-10s enabled=%s\n" "$unit" "$state" "$enabled"
done

info "Deploy complete. Tail live logs with:"
echo "    journalctl -u ahs-mono     -f"
echo "    journalctl -u ahs-streamlit -f"
