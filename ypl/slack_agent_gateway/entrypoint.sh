#!/bin/bash
set -e

source "$(pwd)/scripts/gcp_instance_id.sh"

export CONTAINER_INSTANCE_ID=$(get_container_instance_id)

if [ "$ENVIRONMENT" = "production" ]; then
    export SERVICE_NAME_FOR_LOGGING="slack-agent-gateway"
else
    export SERVICE_NAME_FOR_LOGGING="slack-agent-gateway-$ENVIRONMENT"
fi

exec uvicorn ypl.slack_agent_gateway.server:app --host 0.0.0.0 --port 8080 --loop uvloop --log-config=ypl/uvicorn-logging.yml
