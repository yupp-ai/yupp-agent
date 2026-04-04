#!/bin/bash
# Monolith entrypoint — runs AHS + MCP + gateways in a single uvicorn process.
# Port defaults to 8090, overridable via AHS_PORT.
set -e

export SERVICE_NAME_FOR_LOGGING="yupp-agent-monolith"

exec /opt/yupp-agent/.venv/bin/uvicorn \
    ypl.mono_server.server:app \
    --host 0.0.0.0 \
    --port "${AHS_PORT:-8090}" \
    --loop uvloop \
    --log-config=ypl/uvicorn-logging.yml
