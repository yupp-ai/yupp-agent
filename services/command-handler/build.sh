#!/usr/bin/env bash
# build.sh — compile the AHS BCH proxy binary in-place.
#
# Run once on the VM after syncing the repo (no Docker required).
# The resulting binary is placed at:
#   ypl/agent_harness_service/executors/bin/ahs-command-handler
#
# Requirements: Go 1.22+ must be installed (go is on $PATH).
#
# Usage:
#   cd services/command-handler && bash build.sh
#   # or from anywhere in the repo:
#   bash services/command-handler/build.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
OUTPUT="${REPO_ROOT}/ypl/agent_harness_service/executors/bin/ahs-command-handler"

echo "→ Building AHS BCH proxy"
echo "  source : ${SCRIPT_DIR}"
echo "  output : ${OUTPUT}"

mkdir -p "$(dirname "${OUTPUT}")"

cd "${SCRIPT_DIR}"

# Static binary — no cgo, no external libs required on the target VM.
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  go build \
    -trimpath \
    -ldflags="-s -w" \
    -o "${OUTPUT}" \
    .

echo "✓ Built: ${OUTPUT} ($(du -h "${OUTPUT}" | cut -f1))"
