#!/bin/bash
set -e

source "$(pwd)/scripts/gcp_instance_id.sh"

export CONTAINER_INSTANCE_ID=$(get_container_instance_id)

export SERVICE_NAME_FOR_LOGGING="mcp-server-$ENVIRONMENT"

exec uvicorn ypl.mcp_server.server:app --host 0.0.0.0 --port ${PORT:-8080} --loop uvloop --workers 2 --log-config=ypl/uvicorn-logging.yml
