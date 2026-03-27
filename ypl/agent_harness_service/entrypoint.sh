#!/bin/bash
set -e

export SERVICE_NAME_FOR_LOGGING="agent-harness-service"

exec /opt/yupp-mind/.venv/bin/uvicorn ypl.agent_harness_service.server:app --host 0.0.0.0 --port ${AHS_PORT:-8090} --loop uvloop --log-config=ypl/uvicorn-logging.yml
