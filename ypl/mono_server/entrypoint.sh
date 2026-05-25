#!/bin/bash
# Monolith entrypoint — runs AHS + MCP + gateways in a single uvicorn process.
# Port defaults to 8090, overridable via AHS_PORT.
#
# uvicorn resolution order:
#   1. ${VENV_BIN}/uvicorn        (if VENV_BIN is set and executable)
#   2. /opt/yupp-agent/.venv/bin/uvicorn  (bare-metal VM default — install.sh
#                                          creates this symlink)
#   3. $(command -v uvicorn)      (PATH lookup — Docker images where Poetry
#                                  installs into the system Python, or a
#                                  developer running from an activated venv)
set -euo pipefail

export SERVICE_NAME_FOR_LOGGING="ahs-mono"

# Resolve the uvicorn binary.
DEFAULT_VENV_BIN="/opt/yupp-agent/.venv/bin"
if [ -x "${VENV_BIN:-$DEFAULT_VENV_BIN}/uvicorn" ]; then
    UVICORN="${VENV_BIN:-$DEFAULT_VENV_BIN}/uvicorn"
elif command -v uvicorn >/dev/null 2>&1; then
    UVICORN="$(command -v uvicorn)"
else
    echo "ERROR: uvicorn not found." >&2
    echo "  Tried: \${VENV_BIN:-$DEFAULT_VENV_BIN}/uvicorn and PATH." >&2
    echo "  Set VENV_BIN to the directory containing uvicorn, or install it on PATH." >&2
    exit 1
fi

exec "$UVICORN" \
    ypl.mono_server.server:app \
    --host 0.0.0.0 \
    --port "${AHS_PORT:-8090}" \
    --loop uvloop \
    --log-config=ypl/uvicorn-logging.yml
