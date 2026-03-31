"""Agent specification registry — loaded from DB (primary) and filesystem (fallback).

At startup, all agents from the DB (including on-disk configs synced to DB) are
loaded into _RUNTIME_CONFIGS via load_registry_from_db_async(). This ensures that
both on-disk agents and DB-only agents (e.g. personal agents, eng-raccoon) are
available as subagents.

Filesystem discovery via _discover_agent_specs() remains as a secondary source
for agents that haven't been loaded into _RUNTIME_CONFIGS yet (e.g. test environments
without a DB connection).
"""

import json
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ypl.agent_harness_service.common.constants import AHS_AGENTS_DIR, DEFAULT_MAX_STEPS, DEFAULT_TIMEOUT_S
from ypl.agent_harness_service.common.models import (
    AgentSpec,
    CompactionConfig,
    ExecutorConfig,
    HistoryConfig,
    expand_tool_permissions,
)
from ypl.structured_logger import get_logger

logger = get_logger()

_REGISTRY_LOCK = threading.Lock()
_RUNTIME_CONFIGS: dict[str, AgentSpec] = {}


def _load_agent_spec(name: str, config_path: str) -> AgentSpec | None:
    """Load an agent spec from a config.json file.

    Returns None if the file doesn't contain orchestration fields
    (no ``executor_config`` key).
    """
    try:
        with open(config_path) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load agent config", name=name, error=str(e))
        return None

    # Orchestration-capable configs are identified by the presence of executor_config
    executor_raw = raw.get("executor_config")
    if executor_raw is None:
        return None

    try:
        compaction_raw = executor_raw.get("compaction", {})
        compaction = CompactionConfig(
            enabled=compaction_raw.get("enabled", True),
            model=compaction_raw.get("model", "anthropic/claude-haiku-4-5"),
            prune_protect_steps=compaction_raw.get("prune_protect_steps", 2),
            target_ratio=compaction_raw.get("target_ratio", 0.7),
        )

        history_raw = executor_raw.get("history", {})
        if not isinstance(history_raw, dict):
            # Handle e.g. `"history": false` — treat as enabled toggle
            history_raw = {"enabled": bool(history_raw)}
        history = HistoryConfig(
            enabled=history_raw.get("enabled", True),
        )

        executor = ExecutorConfig(
            type=executor_raw.get("type", "harnessed"),
            model=executor_raw.get("model"),
            harness=executor_raw.get("harness"),
            max_tool_result_chars=executor_raw.get("max_tool_result_chars", 30_000),
            max_tool_result_lines=executor_raw.get("max_tool_result_lines", 1_000),
            compaction=compaction,
            history=history,
        )
        tools = expand_tool_permissions(raw.get("tool_permissions", {"*": "allow"}))

        return AgentSpec(
            name=name,
            description=raw.get("description"),
            executor=executor,
            tools=tools,
            allowed_subagents=raw.get("allowed_subagents", []),
            max_steps=raw.get("max_steps") or raw.get("max_turns") or DEFAULT_MAX_STEPS,
            timeout_s=raw.get("timeout_s", 300),
            temperature=raw.get("temperature"),
            streaming=raw.get("streaming", True),
            model_parameters=raw.get("model_parameters"),
        )
    except (ValidationError, ValueError) as e:
        logger.error("Invalid agent spec", name=name, error=str(e))
        return None


@lru_cache(maxsize=1)
def _discover_agent_specs() -> dict[str, AgentSpec]:
    """Scan AHS_AGENTS_DIR for config.json files with orchestration fields."""
    specs: dict[str, AgentSpec] = {}
    agents_dir = Path(AHS_AGENTS_DIR)

    if not agents_dir.is_dir():
        logger.warning("Agents directory not found", path=AHS_AGENTS_DIR)
        return specs

    for entry in sorted(agents_dir.iterdir()):
        if not entry.is_dir():
            continue
        config_path = entry / "config.json"
        if not config_path.is_file():
            continue
        spec = _load_agent_spec(entry.name, str(config_path))
        if spec:
            specs[entry.name] = spec

    logger.info("Discovered agent specs", count=len(specs), names=list(specs.keys()))
    return specs


def get_agent_spec(name: str) -> AgentSpec | None:
    """Look up an agent spec by name.

    Checks runtime-registered specs first, then filesystem specs.

    Args:
        name: Agent spec name (e.g., 'reviewer', 'fixer').

    Returns:
        AgentSpec if found, None otherwise.
    """
    with _REGISTRY_LOCK:
        if name in _RUNTIME_CONFIGS:
            return _RUNTIME_CONFIGS[name]
    return _discover_agent_specs().get(name)


def list_agent_specs() -> list[AgentSpec]:
    """List all agent specs (filesystem + runtime-registered).

    Returns:
        List of all AgentSpec instances.
    """
    merged = {**_discover_agent_specs()}
    with _REGISTRY_LOCK:
        merged.update(_RUNTIME_CONFIGS)
    return list(merged.values())


def register_agent_config(config: AgentSpec) -> None:
    """Register a custom agent spec at runtime.

    Overwrites any existing runtime spec with the same name. Thread-safe.
    """
    with _REGISTRY_LOCK:
        _RUNTIME_CONFIGS[config.name] = config


def clear_agent_spec_cache() -> None:
    """Clear the filesystem discovery cache. Useful for testing."""
    _discover_agent_specs.cache_clear()


def _db_agent_to_spec(name: str, config_jsonb: dict[str, Any], description: str | None = None) -> AgentSpec | None:
    """Build an AgentSpec from a DB agent's config JSONB dict.

    Mirrors _load_agent_spec() but reads from an in-memory dict instead of a JSON file.
    Returns None if the config lacks an executor_config (not an orchestratable agent).
    """
    executor_raw = config_jsonb.get("executor_config")
    if executor_raw is None:
        return None

    try:
        compaction_raw = executor_raw.get("compaction", {})
        compaction = CompactionConfig(
            enabled=compaction_raw.get("enabled", True),
            model=compaction_raw.get("model", "anthropic/claude-haiku-4-5"),
            prune_protect_steps=compaction_raw.get("prune_protect_steps", 2),
            target_ratio=compaction_raw.get("target_ratio", 0.7),
        )

        history_raw = executor_raw.get("history", {})
        if not isinstance(history_raw, dict):
            history_raw = {"enabled": bool(history_raw)}
        history = HistoryConfig(enabled=history_raw.get("enabled", True))

        executor = ExecutorConfig(
            type=executor_raw.get("type", "harnessed"),
            model=executor_raw.get("model"),
            harness=executor_raw.get("harness"),
            max_tool_result_chars=executor_raw.get("max_tool_result_chars", 30_000),
            max_tool_result_lines=executor_raw.get("max_tool_result_lines", 1_000),
            compaction=compaction,
            history=history,
        )
        tools = expand_tool_permissions(config_jsonb.get("tool_permissions", {"*": "allow"}))

        return AgentSpec(
            name=name,
            description=description,
            executor=executor,
            tools=tools,
            allowed_subagents=config_jsonb.get("allowed_subagents", []),
            max_steps=config_jsonb.get("max_steps") or config_jsonb.get("max_turns") or DEFAULT_MAX_STEPS,
            timeout_s=config_jsonb.get("timeout_s", DEFAULT_TIMEOUT_S),
            temperature=config_jsonb.get("temperature"),
            streaming=config_jsonb.get("streaming", True),
        )
    except (ValidationError, ValueError) as e:
        logger.error("Invalid agent spec from DB", name=name, error=str(e))
        return None


async def load_registry_from_db_async() -> int:
    """Load all DB agents into _RUNTIME_CONFIGS at startup.

    Queries all Agent rows from the DB and registers AgentSpec instances for each.
    On-disk agents are overwritten with their DB representation (identical data
    since _sync_agents_to_db just ran). DB-only agents (personal agents, etc.) are
    newly added to the registry so they become available as subagents.

    Returns:
        Number of agent specs registered.
    """
    try:
        from sqlmodel import select as sa_select

        from ypl.backend.db import get_async_session
        from ypl.db.agent_harness import Agent as DBAgent

        async with get_async_session() as db:
            result = await db.exec(sa_select(DBAgent))
            all_agents = result.all()
    except Exception:
        logger.error("Failed to load agent registry from DB — registry may be incomplete", exc_info=True)
        return 0

    registered = 0
    for agent in all_agents:
        spec = _db_agent_to_spec(agent.name, agent.config or {}, agent.description)
        if spec:
            register_agent_config(spec)
            registered += 1

    logger.info(
        "Loaded agent specs from DB",
        registered=registered,
        total=len(all_agents),
        skipped=len(all_agents) - registered,
    )
    return registered


# Backward-compatible alias used by local_mcp_server.py
list_predefined_agents = list_agent_specs
