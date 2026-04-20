"""Agent create and edit operations."""

from typing import Any

from ypl.agent_harness_service.common.config import validate_agent_name
from ypl.agent_harness_service.common.types import (
    AgentCreateRequest,
    AgentCreateResponse,
    AgentEditRequest,
    AgentEditResponse,
)
from ypl.agent_harness_service.service.resolvers import _resolve_agent
from ypl.backend.config import settings
from ypl.backend.db import get_async_session
from ypl.db.agent_harness import Agent, AgentExecutorType
from ypl.db.users import User, UserStatus, UserType
from ypl.structured_logger import get_logger

logger = get_logger()


async def create_agent(request: AgentCreateRequest) -> AgentCreateResponse:
    """Create a new agent: register in DB with config stored as JSONB."""
    validate_agent_name(request.name)

    # Build config payload (stored in the Agent.config JSONB column)
    config_data: dict[str, Any] = {
        "default_repo": request.default_repo,
        "max_turns": request.max_turns,
        "max_budget_usd": request.max_budget_usd,
        "feedback_probability": request.feedback_probability,
        "feedback_min_turns": request.feedback_min_turns,
        "timeout_s": request.timeout_s,
        "executor_config": {
            "type": request.executor_config.type,
            "model": request.executor_config.model,
        },
        "tool_permissions": request.tool_permissions,
        "allowed_subagents": request.allowed_subagents,
        "allowed_gateways": request.allowed_gateways,
        "sandbox": {
            "enabled": request.sandbox.enabled,
            "autoAllowBashIfSandboxed": request.sandbox.auto_allow_bash_if_sandboxed,
            "bwrapEnabled": request.sandbox.bwrap_enabled,
        },
    }

    if request.role_md:
        config_data["role_md"] = request.role_md
    if request.soul_md:
        config_data["soul_md"] = request.soul_md

    async with get_async_session() as session:
        existing = await _resolve_agent(session, request.name)
        if existing:
            raise ValueError(f"Agent already exists: {request.name}")

        agent = Agent(
            name=request.name,
            display_name=request.display_name or request.name,
            description=request.description,
            executor_type=request.executor_config.type.upper(),
            executor_model=request.executor_config.model,
            config=config_data,
            creator_user_id=request.user_id,
            additional_system_prompt=request.additional_system_prompt,
        )
        session.add(agent)
        await session.flush()  # populate agent.agent_id without committing

        # Create a corresponding User identity row for this agent in the same transaction.
        # If either insert fails, neither is committed.
        # Use agent_id (not name) for uniqueness: names can collide or be
        # reused; agent_id is stable and UUID-unique. Use AGENT_USER_EMAIL_DOMAIN
        # (distinct from ALLOWED_EMAIL_DOMAINS) to keep agent identities out of
        # human employee flows (e.g. _resolve_personal_agent_for_user gates on
        # the allowed human domain suffix).
        user = User(
            user_id=str(agent.agent_id),
            name=f"agent:{agent.name}",
            email=f"agent-{agent.agent_id}@{settings.AGENT_USER_EMAIL_DOMAIN}",
            user_type=UserType.AGENT,
            status=UserStatus.ACTIVE,
        )
        session.add(user)
        await session.flush()  # ensure User row exists before FK reference
        agent.agent_user_id = str(agent.agent_id)
        await session.commit()

    logger.info("Created agent", name=request.name, user_id=request.user_id)
    return AgentCreateResponse(name=request.name, status="created")


async def edit_agent(request: AgentEditRequest) -> AgentEditResponse:
    """Edit an existing agent's configuration in DB."""
    async with get_async_session() as session:
        agent = await _resolve_agent(session, request.name)
        if not agent:
            raise ValueError(f"Agent not found: {request.name}")

        # Ownership check: only the creator can edit their agent.
        # Agents without a creator_user_id are filesystem/global agents — not editable via API.
        if not agent.creator_user_id:
            raise PermissionError(f"Agent '{request.name}' is a global agent and cannot be edited via API")
        if agent.creator_user_id != request.user_id:
            raise PermissionError(f"Only the agent creator can edit agent '{request.name}'")

        # Update top-level fields
        # TODO: `is not None` pattern means fields can't be cleared back to None once set.
        # Add a `fields_to_clear` list or sentinel value if clearing becomes a requirement.
        if request.display_name is not None:
            agent.display_name = request.display_name
        if request.description is not None:
            agent.description = request.description
        if request.executor_config is not None:
            agent.executor_type = AgentExecutorType(request.executor_config.type.upper())
            agent.executor_model = request.executor_config.model
        if request.additional_system_prompt is not None:
            agent.additional_system_prompt = request.additional_system_prompt

        # Update config JSONB — merge provided fields into existing config
        config_data = dict(agent.config) if agent.config else {}
        if request.executor_config is not None:
            config_data["executor_config"] = {
                "type": request.executor_config.type,
                "model": request.executor_config.model,
            }
        if request.tool_permissions is not None:
            config_data["tool_permissions"] = request.tool_permissions
        if request.allowed_subagents is not None:
            config_data["allowed_subagents"] = request.allowed_subagents
        if request.default_repo is not None:
            config_data["default_repo"] = request.default_repo
        if request.sandbox is not None:
            config_data["sandbox"] = {
                "enabled": request.sandbox.enabled,
                "autoAllowBashIfSandboxed": request.sandbox.auto_allow_bash_if_sandboxed,
                "bwrapEnabled": request.sandbox.bwrap_enabled,
            }
        if request.max_turns is not None:
            config_data["max_turns"] = request.max_turns
        if request.max_budget_usd is not None:
            config_data["max_budget_usd"] = request.max_budget_usd
        if request.timeout_s is not None:
            config_data["timeout_s"] = request.timeout_s
        if request.allowed_gateways is not None:
            config_data["allowed_gateways"] = request.allowed_gateways
        if request.role_md is not None:
            config_data["role_md"] = request.role_md
        if request.soul_md is not None:
            config_data["soul_md"] = request.soul_md

        agent.config = config_data
        session.add(agent)
        await session.commit()

    logger.info("Edited agent", name=request.name, user_id=request.user_id)
    return AgentEditResponse(name=request.name, status="updated")
