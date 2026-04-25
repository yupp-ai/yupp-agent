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
DATA_DIR="${DATA_DIR:-/data/ahs}"
APP_USER="${APP_USER:-ahs}"
SERVICES=(ahs-mono ahs-streamlit artifact-viewer couch)
# Sub-apps with their own pyproject / venv. Each gets ``pip install -e`` on
# every deploy so code changes take effect without a separate step.
SUBAPPS=(apps/artifact-viewer)
# Bun apps. Each gets ``bun install --production && bun run build`` on every
# deploy so code changes take effect without a separate step.
BUN_APPS=(apps/couch)

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

# Must run git as ${APP_USER} — root-running-git on an ahs-owned checkout
# trips git's "dubious ownership" guard.
info "Now at $(sudo -u "$APP_USER" git -C "$INSTALL_DIR" log -1 --oneline)"

# --- 2. Sync Python deps (monolith) ----------------------------------------
info "poetry install (cheap if nothing changed)…"
sudo -u "$APP_USER" env INSTALL_DIR="$INSTALL_DIR" bash -c '
    cd "$INSTALL_DIR"
    poetry install --no-root --without dev --compile
'

# --- 2b. Sync sub-app deps (artifact-viewer, etc.) -------------------------
for subapp in "${SUBAPPS[@]}"; do
    app_dir="${INSTALL_DIR}/${subapp}"
    app_venv="${app_dir}/.venv"
    if [[ ! -d "$app_dir" ]]; then
        warn "Sub-app ${subapp} not present in repo — skipping."
        continue
    fi
    # Key the "venv exists" check on bin/pip (not bin/python) so a
    # half-created venv from a prior failed run is auto-repaired.
    if [[ ! -x "${app_venv}/bin/pip" ]]; then
        if [[ -d "$app_venv" ]]; then
            warn "Sub-app venv at ${app_venv} is incomplete (no pip) — recreating."
            rm -rf "$app_venv"
        fi
        info "Creating sub-app venv at ${app_venv}… (install.sh should have done this)"
        sudo -u "$APP_USER" python3.12 -m venv "$app_venv"
    fi
    info "pip install -e ${subapp} (cheap if nothing changed)…"
    sudo -u "$APP_USER" "${app_venv}/bin/pip" install --quiet -e "$app_dir"
done

# --- 2c. Sync Bun apps (couch, etc.) ---------------------------------------
for bunapp in "${BUN_APPS[@]}"; do
    app_dir="${INSTALL_DIR}/${bunapp}"
    if [[ ! -d "$app_dir" ]]; then
        warn "Bun app ${bunapp} not present in repo — skipping."
        continue
    fi
    if ! command -v bun >/dev/null 2>&1; then
        die "bun not found on PATH; install Bun system-wide before deploying ${bunapp}."
    fi
    info "bun install + build for ${bunapp} (cheap if nothing changed)…"
    sudo -u "$APP_USER" bash -c "
        set -euo pipefail
        cd '$app_dir'
        bun install --production --frozen-lockfile
        bun run build
    " || die "bun install/build for ${bunapp} failed."
done

# --- 3. Sync systemd unit files --------------------------------------------
# Unit files can live under deploy/systemd/ (main services) OR
# apps/*/deploy/*.service (sub-apps and Bun apps). Each service name maps
# to whichever source file exists.
UNITS_CHANGED=0
unit_source_for() {
    local unit="$1"
    local main="${INSTALL_DIR}/deploy/systemd/${unit}.service"
    if [[ -f "$main" ]]; then
        echo "$main"; return 0
    fi
    for subapp in "${SUBAPPS[@]}" "${BUN_APPS[@]}"; do
        local alt="${INSTALL_DIR}/${subapp}/deploy/${unit}.service"
        if [[ -f "$alt" ]]; then
            echo "$alt"; return 0
        fi
    done
    return 1
}
for unit in "${SERVICES[@]}"; do
    if ! src=$(unit_source_for "$unit"); then
        warn "Skipping ${unit}: no .service file found in repo."
        continue
    fi
    dst="/etc/systemd/system/${unit}.service"
    if ! cmp -s "$src" "$dst"; then
        info "Updating ${dst} (source: ${src#$INSTALL_DIR/})"
        cp "$src" "$dst"
        UNITS_CHANGED=1
    fi
done

# Also sync the periodic pull service + timer (and any other timers we add
# later). Timer units don't fit in SERVICES (which is used for `systemctl
# restart`), so handle them here.
for aux in "ahs-pull-agent-repos.service" "ahs-pull-agent-repos.timer"; do
    src="${INSTALL_DIR}/deploy/systemd/${aux}"
    dst="/etc/systemd/system/${aux}"
    if [[ -f "$src" ]] && ! cmp -s "$src" "$dst"; then
        info "Updating ${dst}"
        cp "$src" "$dst"
        UNITS_CHANGED=1
    fi
done

if [[ $UNITS_CHANGED -eq 1 ]]; then
    info "Unit files changed — systemctl daemon-reload"
    systemctl daemon-reload
    # Make sure the pull timer is enabled and running — no-op if already.
    if [[ -f /etc/systemd/system/ahs-pull-agent-repos.timer ]]; then
        systemctl enable --now ahs-pull-agent-repos.timer >/dev/null 2>&1 || true
    fi
fi

# --- 4. Database migrations ------------------------------------------------
# Run alembic in a transient systemd unit that reuses the service's
# EnvironmentFile. Bash `source .env` can't parse the JSON connection
# strings (commas inside {...} get interpreted as shell separators); systemd
# parses EnvironmentFile= correctly since it's the same format the service uses.
info "alembic upgrade head (no-op if already at head)…"
systemd-run --wait --quiet --pipe \
    --property=User="${APP_USER}" \
    --property=Group="${APP_USER}" \
    --property=EnvironmentFile="${DATA_DIR}/.env" \
    --property=WorkingDirectory="${INSTALL_DIR}" \
    "${INSTALL_DIR}/.venv/bin/python" -m alembic -c alembic.ini upgrade head \
    || die "alembic upgrade head failed. Fix the schema issue before retrying."

# --- 5. Restart services ---------------------------------------------------
info "Restarting: ${SERVICES[*]}"
systemctl restart "${SERVICES[@]}"

# --- 6. Pull agent-workspace repos immediately ------------------------------
# ``/data/ahs/repos/*`` drives what agent sessions see (their .mcp.json +
# skill files are seeded from there). The systemd timer pulls these every
# 5 min, but operators expect their deploy to be effective right away, so
# kick one pull now. Failure is non-fatal — the next timer tick retries.
if systemctl list-unit-files | grep -q '^ahs-pull-agent-repos\.service'; then
    info "Pulling agent-workspace repos (/data/ahs/repos/*)…"
    if ! systemctl start ahs-pull-agent-repos.service; then
        warn "Agent-repo pull failed — the 5-min timer will retry. Check: journalctl -u ahs-pull-agent-repos"
    fi
fi

# --- 7. Status summary -----------------------------------------------------
echo
for unit in "${SERVICES[@]}"; do
    state=$(systemctl is-active "$unit" || true)
    enabled=$(systemctl is-enabled "$unit" || true)
    printf "  %-24s active=%-10s enabled=%s\n" "$unit" "$state" "$enabled"
done
if systemctl list-unit-files | grep -q '^ahs-pull-agent-repos\.timer'; then
    state=$(systemctl is-active ahs-pull-agent-repos.timer || true)
    enabled=$(systemctl is-enabled ahs-pull-agent-repos.timer || true)
    printf "  %-24s active=%-10s enabled=%s\n" "ahs-pull-agent-repos.timer" "$state" "$enabled"
fi

info "Deploy complete. Tail live logs with:"
echo "    journalctl -u ahs-mono              -f"
echo "    journalctl -u ahs-streamlit         -f"
echo "    journalctl -u ahs-pull-agent-repos  -f"
