"""Import all models so Alembic can detect them for autogenerate."""

from ypl.db import (
    agent_harness,
    agent_memory_index,
    mcp,
    rbac,
    slack_agent,
    slack_oauth_token,
    users,
    yuppaste_comments,
)

all_models = [
    # Please keep sorted.
    agent_harness,
    agent_memory_index,
    mcp,
    rbac,
    slack_agent,
    slack_oauth_token,
    users,
    yuppaste_comments,
]
