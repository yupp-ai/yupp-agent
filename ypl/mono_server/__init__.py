"""Yupp Agent Platform — monolith server package.

Composes AHS, SAG, and MCP into a single FastAPI application so the
entire agent platform can run as one process (one-box deployment).

Route layout when running as the monolith:
  /ahs/*          — Agent Harness Service
  /mcp/harness/*  — Harness MCP (for agents)
  /mcp/*          — Yuppster MCP (for developers)
  /gw/slack/*     — Slack gateway (if GATEWAY_SLACK_ENABLED=true)
  /health         — Liveness probe
"""
