#!/bin/bash
set -e

# Legacy GCP helper — present on the prod/staging Cloud Run images but pruned
# from the open-source tree. Source it when present; otherwise fall back to a
# deterministic value so the entrypoint still boots (one-box / Mac / self-hosted).
if [ -f "$(pwd)/scripts/gcp_instance_id.sh" ]; then
    # shellcheck disable=SC1091
    source "$(pwd)/scripts/gcp_instance_id.sh"
    export CONTAINER_INSTANCE_ID=$(get_container_instance_id)
else
    export CONTAINER_INSTANCE_ID="${CONTAINER_INSTANCE_ID:-local}"
fi

export SERVICE_NAME_FOR_LOGGING="mcp-server-$ENVIRONMENT"

exec uvicorn ypl.mcp_server.server:app --host 0.0.0.0 --port ${PORT:-8080} --loop uvloop --workers 2 --log-config=ypl/uvicorn-logging.yml
