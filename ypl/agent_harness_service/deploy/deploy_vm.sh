#!/bin/bash
#
# Deploy AHS to the VM.
#
# Called by the GitHub Actions workflow after scp'ing the .env file.
# Can also be run manually on the VM.
#
# The workflow checks out the target commit before running this script.
# For manual use, checkout the desired commit first, then run:
#
# Usage:
#   sudo bash deploy_vm.sh              # from the workflow (git already checked out)
#   sudo bash deploy_vm.sh <GIT_SHA>    # manual: fetches + checks out, then deploys
#
set -euo pipefail

REPO="/opt/yupp-agent"
SERVICE_USER="ahs"
GIT_SHA="${1:-}"

echo "=== AHS VM Deploy ==="
echo "  repo: ${REPO}"
echo "  HEAD: $(cd ${REPO} && git rev-parse --short HEAD)"

# 0. Optional: checkout target commit (for manual use)
if [ -n "$GIT_SHA" ]; then
    cd "$REPO"
    sudo -u ${SERVICE_USER} git fetch origin
    sudo -u ${SERVICE_USER} git checkout --force "$GIT_SHA"
    echo "[0] Checked out ${GIT_SHA:0:7}"
fi

# 1. Install .env if staged by workflow
if [ -f /tmp/ahs.env ]; then
    cp /tmp/ahs.env /data/ahs/.env
    chown ${SERVICE_USER}:${SERVICE_USER} /data/ahs/.env
    chmod 600 /data/ahs/.env
    rm -f /tmp/ahs.env
    echo "[1/5] .env installed"
else
    echo "[1/5] .env skipped (no /tmp/ahs.env)"
fi

# 2. Install Python deps
sudo -u ${SERVICE_USER} bash -c "cd ${REPO} && .venv/bin/pip install 'setuptools<80' && .venv/bin/poetry lock && .venv/bin/poetry install --no-root"
echo "[2/5] Python deps installed"

# 3. Build Go binary (if Go is available)
if command -v go &> /dev/null; then
    sudo -u ${SERVICE_USER} bash "${REPO}/services/command-handler/build.sh"
    echo "[3/5] Go binary built"
else
    echo "[3/5] Go binary skipped (go not installed)"
fi

# 4. Sync configs
sudo -u ${SERVICE_USER} bash "${REPO}/ypl/agent_harness_service/deploy/sync_configs.sh" --no-pull
echo "[4/5] Configs synced"

# 5. Restart service
systemctl restart ahs
echo "[5/5] AHS restarted"

echo "=== Deploy complete ==="
