"""Yupp Agent Platform — monolith server package.

Composes AHS, SAG, and MCP into a single FastAPI application so the
entire agent platform can run as one process (one-box deployment).

Two master flags in :class:`~ypl.mono_server.config.MonoConfig` gate the
optional surfaces; both default to ``False`` so the monolith boots as a
pure AHS process out of the box.

Route layout when running as the monolith (full feature set):
  /ahs/*          — Agent Harness Service                  (always)
  /mcp/harness/*  — Harness MCP                            (always; agents auth
                    via x-ahs-token / Bearer <secret>:<session_id>)
  /mcp/agcouch/*  — Agcouch MCP                            (gated on
                    ``AHS_MONO_ENABLE_MCP``; developers auth via Bearer
                    yupp_dev_*)
  /gw/slack/*     — Slack gateway                          (gated on both
                    ``AHS_MONO_ENABLE_GATEWAY_SERVICE`` AND
                    ``GATEWAY_SLACK_ENABLED``; default-on sub-flag)
  /gw/github/*    — GitHub webhook gateway                 (gated on both
                    ``AHS_MONO_ENABLE_GATEWAY_SERVICE`` AND
                    ``GATEWAY_GITHUB_ENABLED``; default-off sub-flag)
  /health         — Liveness probe                         (always)

Default deployment (both master flags false): only /ahs/*, /mcp/harness/*,
and /health are mounted. AHS sessions still have full tool access via the
harness MCP (shared and external-data tools dual-register there per PR #300).

See ``DEPLOYMENT.md`` for the upgrade-from-previous-default story.
"""
