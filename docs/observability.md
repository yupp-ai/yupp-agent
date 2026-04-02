# Observability — Logging & Metrics

This document describes the logging architecture for the Yupp Agent Platform monolith
(`ypl/mono_server`) and provides deployment-specific instructions for systemd, MacBook
local development, and the future Prometheus metrics path.

---

## Structured Logging

All services use [structlog](https://www.structlog.org/) with a shared processor pipeline
defined in `ypl/structured_logger.py`.  Every log record automatically includes:

| Field | Source | Example |
|---|---|---|
| `timestamp` | `TimeStamper(fmt="iso")` | `2026-04-02T09:00:00.123` |
| `service` | `SERVICE_NAME` env var | `"mono_server"`, `"ahs"`, `"sag"` |
| `level` | structlog level method | `"info"`, `"warning"`, `"error"` |
| `message` / `event` | log call | `"Session started"` |
| `func_name` | callsite | `"handle_request"` |
| `module` | callsite | `"ypl.agent_harness_service.service"` |
| `thread` / `thread_name` | callsite | `"MainThread"` |

### Service Tagging

Set the `SERVICE_NAME` environment variable to identify the running component in every
log line.  Recommended values:

```
SERVICE_NAME=mono_server   # full monolith (AHS + SAG + MCP)
SERVICE_NAME=ahs           # standalone Agent Harness Service
SERVICE_NAME=sag           # standalone Slack Agent Gateway
SERVICE_NAME=mcp           # standalone MCP Server
```

If `SERVICE_NAME` is not set, the field defaults to `"yupp-agent"`.

---

## Output Formats

The output format is selected via the `LOG_FORMAT` environment variable.

### `LOG_FORMAT=json` — Machine-readable NDJSON

One JSON object per line, written to stdout.  Suitable for:

- **systemd journal** — captured automatically; query with `journalctl`
- **Loki / Promtail** — configure `pipeline_stages: [json: ...]` to parse fields
- **CloudWatch / Datadog** — any aggregator that ingests NDJSON

Example line:

```json
{"message": "Session started", "service": "mono_server", "timestamp": "2026-04-02T09:00:00.123", "level": "info", "session_id": "abc123"}
```

### `LOG_FORMAT=pretty` (default) — Human-readable console output

Coloured output via structlog's `ConsoleRenderer`.  Ideal for interactive terminal
sessions on a MacBook or in CI logs.

### `USE_GOOGLE_CLOUD_LOGGING=true` — Google Cloud Logging

Sends logs directly to Cloud Logging via the background-thread transport.  All
google-cloud-logging imports are deferred inside `init_google_cloud_logger()` so the
rest of the codebase can be imported without the package installed or credentials
present.

---

## Deployment Recipes

### systemd on a Linux VM / bare-metal box

```ini
# /etc/systemd/system/yupp-agent.service
[Unit]
Description=Yupp Agent Platform (monolith)
After=network.target

[Service]
User=yupp
WorkingDirectory=/opt/yupp-agent
EnvironmentFile=/opt/yupp-agent/.env
Environment="SERVICE_NAME=mono_server"
Environment="LOG_FORMAT=json"
ExecStart=/opt/yupp-agent/.venv/bin/uvicorn \
    ypl.mono_server.server:app \
    --host 0.0.0.0 \
    --port 8090 \
    --workers 1
Restart=on-failure
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Query logs:

```bash
# Tail live logs
journalctl -u yupp-agent -f

# Filter by log level
journalctl -u yupp-agent -o json | jq 'select(.level == "error")'

# Filter by service field (useful in multi-service setups)
journalctl -u yupp-agent -o json | jq 'select(.service == "ahs")'

# Search by session ID
journalctl -u yupp-agent -o json | jq 'select(.session_id == "abc123")'
```

### MacBook local development

No additional configuration needed.  The default `LOG_FORMAT=pretty` produces
coloured console output.

```bash
# Start the monolith
uvicorn ypl.mono_server.server:app --port 8090 --reload

# Or with JSON output (e.g. to test aggregator pipelines locally)
LOG_FORMAT=json uvicorn ypl.mono_server.server:app --port 8090

# Parse with jq
LOG_FORMAT=json uvicorn ypl.mono_server.server:app --port 8090 2>&1 | jq .
```

### Google Cloud Run / GCP VM (production)

```bash
USE_GOOGLE_CLOUD_LOGGING=true
SERVICE_NAME=mono_server
GCP_PROJECT_ID=yupp-llms
```

Logs appear in Cloud Logging under the log name matching `GCP_PROJECT_ID`.
The `service` field is also written to Cloud Logging labels via
`TraceAndProcessLoggingFilter`.

---

## Metrics — `/metrics` Endpoint

The monolith exposes a **stub** Prometheus metrics endpoint at `GET /metrics`.

```
HTTP/1.1 200 OK
Content-Type: text/plain; version=0.0.4; charset=utf-8

# HELP yupp_agent_up Agent platform is running
# TYPE yupp_agent_up gauge
yupp_agent_up 1
```

The content-type already matches Prometheus's scrape format, so a scrape config like
the following will work today:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: yupp_agent
    static_configs:
      - targets: ["localhost:8090"]
```

### Roadmap — adding real metrics

1. Add `prometheus-client` to `pyproject.toml`:
   ```toml
   prometheus-client = "^0.20"
   ```
2. Define collectors (counters, histograms, gauges) in a new module, e.g.
   `ypl/mono_server/metrics.py`.
3. Replace the hardcoded stub body in `server.py` with:
   ```python
   from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
   content = generate_latest()
   return Response(content=content, media_type=CONTENT_TYPE_LATEST)
   ```
4. Optionally add
   [`opentelemetry-exporter-prometheus`](https://pypi.org/project/opentelemetry-exporter-prometheus/)
   to bridge existing OpenTelemetry instrumentation (SQLAlchemy, FastAPI) into
   the Prometheus registry.

---

## No Hard Dependency on Google Cloud Logging

The google-cloud-logging package imports are **deferred** inside
`init_google_cloud_logger()` in `ypl/loggers/config.py`.  This means:

- The module can be imported without `google-cloud-logging` installed.
- `USE_GOOGLE_CLOUD_LOGGING=false` (the default) never touches GCL code paths.
- Any `ImportError` or credential error inside `init_google_cloud_logger()` is
  caught and the logger falls back to `init_console_logger()` automatically.

Similarly, `ypl/loggers/gcloud_utils.py` (which inherits from `CloudLoggingFilter`)
is only imported inside `init_google_cloud_logger()`, keeping the non-GCL path
completely free of google-cloud-logging at runtime.
