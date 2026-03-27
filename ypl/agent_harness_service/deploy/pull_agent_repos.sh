#!/bin/bash
#
# Pull all repos in /data/repos/ (agent read-only checkouts).
#
# Runs the Python pull_repos script which does `git pull` on each repo
# directory (yupp-mind, yupp-soul, yupp-head, etc.).
#
# Run as: sudo -u ahs bash pull_agent_repos.sh
#
set -euo pipefail

SERVICE_REPO="/opt/yupp-mind"
VENV_PYTHON="${SERVICE_REPO}/.venv/bin/python"

cd "$SERVICE_REPO"
"$VENV_PYTHON" -m ypl.agent_harness_service.scripts.pull_repos
