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

if [ "$ENVIRONMENT" = "production" ]; then
    export SERVICE_NAME_FOR_LOGGING="slack-agent-gateway"
else
    export SERVICE_NAME_FOR_LOGGING="slack-agent-gateway-$ENVIRONMENT"
fi

exec uvicorn ypl.slack_agent_gateway.server:app --host 0.0.0.0 --port 8080 --loop uvloop --log-config=ypl/uvicorn-logging.yml
