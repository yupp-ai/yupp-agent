"""Shared Slack helpers used across AHS, SAG, and the MCP server.

Contains the OpsBot singleton clients (the workspace-wide "default" Slack app
used for reads and write fallback) and helpers for resolving which bot should
service a given agent's write request.
"""

from ypl.slack_common.ops_bot import (
    agent_has_slack_presence,
    get_ops_bot_read_client,
    get_ops_bot_user_client,
    get_ops_bot_write_client,
    resolve_display_name,
)

__all__ = [
    "agent_has_slack_presence",
    "get_ops_bot_read_client",
    "get_ops_bot_user_client",
    "get_ops_bot_write_client",
    "resolve_display_name",
]
