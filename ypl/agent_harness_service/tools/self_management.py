"""Personal agent self-management tools for the harness MCP server.

Provides tools for personal agents (and agents with explicit permission) to
read and update their own additional system prompts, enabling persistent
persona customization.
"""

from __future__ import annotations
from typing import Any

from sqlalchemy import text

from ypl.agent_harness_service.tools.mcp_instance import mcp
from ypl.agent_harness_service.tools.subagents import _resolve_current_personal_agent
from ypl.backend.db import get_async_session
from ypl.structured_logger import get_logger

logger = get_logger()


@mcp.tool(
    name="read_self_system_prompt",
    description=(
        "Read your own additional system prompt (persona, identity, preferences). "
        "Use this to check what instructions define your personality and behavior."
    ),
)
async def read_self_system_prompt_tool(session_id: str = "") -> dict[str, Any]:
    """Read the current agent's additional_system_prompt from the database.

    Args:
        session_id: Optional session ID override (auto-resolved from context).

    Returns:
        Dict with the agent name and current additional_system_prompt content.
    """
    agent_name, err = await _resolve_current_personal_agent(session_id, tool_name="read_self_system_prompt")
    if err:
        return {"error": err}
    assert agent_name is not None

    async with get_async_session() as db_session:
        result = await db_session.execute(
            text("SELECT additional_system_prompt FROM agents WHERE name = :name"),
            {"name": agent_name},
        )
        row = result.first()
        if not row:
            return {"error": f"Agent '{agent_name}' not found"}

    return {
        "agent_name": agent_name,
        "additional_system_prompt": row[0] or "",
    }


@mcp.tool(
    name="update_self_system_prompt",
    description=(
        "Update your own additional system prompt (persona, identity, preferences). "
        "Use this when the user asks you to change fundamental aspects of your personality, "
        "how you address them, or core interaction style. The full prompt must be provided — "
        "this replaces the existing content entirely."
    ),
)
async def update_self_system_prompt_tool(
    new_prompt: str,
    session_id: str = "",
) -> dict[str, Any]:
    """Update the current agent's additional_system_prompt in the database.

    Args:
        new_prompt: The complete new additional system prompt content.
        session_id: Optional session ID override (auto-resolved from context).

    Returns:
        Dict with success status and the agent name.
    """
    agent_name, err = await _resolve_current_personal_agent(session_id, tool_name="update_self_system_prompt")
    if err:
        return {"error": err}
    assert agent_name is not None

    if not new_prompt.strip():
        return {"error": "new_prompt cannot be empty"}

    max_prompt_length = 10_000
    if len(new_prompt) > max_prompt_length:
        return {"error": f"new_prompt exceeds maximum length ({len(new_prompt)} > {max_prompt_length} chars)"}

    logger.info(
        "MCP tool: update_self_system_prompt",
        agent_name=agent_name,
        prompt_length=len(new_prompt),
    )

    async with get_async_session() as db_session:
        result = await db_session.execute(
            text("UPDATE agents SET additional_system_prompt = :prompt WHERE name = :name RETURNING name"),
            {"prompt": new_prompt, "name": agent_name},
        )
        if not result.first():
            return {"error": f"Agent '{agent_name}' not found"}
        await db_session.commit()

    return {
        "success": True,
        "agent_name": agent_name,
        "message": f"System prompt updated for '{agent_name}'. Changes take effect on the next session.",
    }
