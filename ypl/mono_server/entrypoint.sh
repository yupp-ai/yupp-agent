#!/bin/bash
# Monolith entrypoint — runs AHS + MCP + gateways in a single uvicorn process.
# Port defaults to 8090, overridable via AHS_PORT.
#
# VENV_BIN: directory containing the uvicorn binary.
#   - Bare-metal default: /opt/yupp-agent/.venv/bin  (set by install.sh symlink)
#   - Docker: /opt/yupp-agent/.venv/bin  (same — base image sets up venv here)
#   Override via: VENV_BIN=/path/to/venv/bin ./entrypoint.sh
set -euo pipefail

export SERVICE_NAME_FOR_LOGGING="ahs-mono"

exec "${VENV_BIN:-/opt/yupp-agent/.venv/bin}/uvicorn" \
    ypl.mono_server.server:app \
    --host 0.0.0.0 \
    --port "${AHS_PORT:-8090}" \
    --loop uvloop \
    --log-config=ypl/uvicorn-logging.yml
