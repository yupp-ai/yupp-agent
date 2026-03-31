"""Add required_tools to bizbot and hercule-poirot agent configs

Revision ID: c4f9a1b2d3e5
Revises: ab0aa44ff305
Create Date: 2026-03-31 00:00:00.000000+00:00

Populates the required_tools list in the agents.config JSONB for CRON agents
that don't have on-disk config.json files (bizbot, hercule-poirot). The harness
reads this field at session start and injects a Phase 0 ToolSearch instruction
into the system prompt so agents pre-load all deferred MCP tools in one call,
eliminating 1-2 tool-discovery turns per session.
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f9a1b2d3e5"
down_revision: str | None = "ab0aa44ff305"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AGENT_REQUIRED_TOOLS: dict[str, list[str]] = {
    "bizbot": [
        "mcp__yuppster-mcp-server__get_agent_memory",
        "mcp__yuppster-mcp-server__store_agent_memory",
        "mcp__yuppster-mcp-server__create_yuppaste",
        "mcp__yuppster-mcp-server__search_agent_memory",
        "mcp__yuppster-mcp-server__add_artifact",
        "mcp__harness__send_slack_message",
    ],
    "hercule-poirot": [
        "mcp__yuppster-mcp-server__get_sentry_issue_details",
        "mcp__yuppster-mcp-server__search_slack",
        "mcp__yuppster-mcp-server__read_slack_thread",
        "mcp__yuppster-mcp-server__create_yuppaste",
        "mcp__harness__send_slack_message",
        "mcp__yuppster-mcp-server__search_agent_memory",
    ],
}


def upgrade() -> None:
    conn = op.get_bind()
    for agent_name, tools in _AGENT_REQUIRED_TOOLS.items():
        conn.execute(
            sa.text(
                """
                UPDATE agents
                SET config = COALESCE(config, '{}'::jsonb)
                          || jsonb_build_object('required_tools', CAST(:tools AS jsonb))
                WHERE name = :name
                """
            ),
            {"name": agent_name, "tools": json.dumps(tools)},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for agent_name in _AGENT_REQUIRED_TOOLS:
        conn.execute(
            sa.text("UPDATE agents SET config = config - 'required_tools' WHERE name = :name"),
            {"name": agent_name},
        )
