#!/bin/bash
# Artifact viewer entrypoint — read-only web UI for AHS textual artifacts.
# Port defaults to 8095, overridable via VIEWER_PORT.
#
# uvicorn resolution order (mirrors ypl/mono_server/entrypoint.sh):
#   1. ${VENV_BIN}/uvicorn        (if VENV_BIN is set and executable)
#   2. /opt/yupp-agent/apps/artifact-viewer/.venv/bin/uvicorn
#                                  (bare-metal VM default — install.sh
#                                   creates this venv with the sub-app
#                                   installed editable)
#   3. $(command -v uvicorn)      (PATH lookup — Docker images where the
#                                  sub-app is `pip install -e`'d into the
#                                  same system Python as the mono server)
set -euo pipefail

export SERVICE_NAME_FOR_LOGGING="artifact-viewer"

DEFAULT_VENV_BIN="/opt/yupp-agent/apps/artifact-viewer/.venv/bin"
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
    artifact_viewer.app:app \
    --host "${VIEWER_HOST:-0.0.0.0}" \
    --port "${VIEWER_PORT:-8095}" \
    --loop uvloop \
    --no-server-header
