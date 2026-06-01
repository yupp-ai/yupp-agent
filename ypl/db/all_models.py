"""Import all models so Alembic can detect them for autogenerate."""

from ypl.db import (
    agent_harness,
    agent_memory_index,
    artifact_comments,
    external_mcp,
    mcp,
    rbac,
    slack_agent,
    slack_oauth_token,
    users,
)

all_models = [
    # Please keep sorted.
    agent_harness,
    agent_memory_index,
    artifact_comments,
    external_mcp,
    mcp,
    rbac,
    slack_agent,
    slack_oauth_token,
    users,
]
