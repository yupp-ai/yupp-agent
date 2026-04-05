"""Tests for ypl.agent_harness_service.common.agent_registry.

Covers:
- _load_agent_spec (file-based: valid, missing executor_config, parse errors)
- _db_agent_to_spec (in-memory: valid, no executor_config, validation errors)
- register_agent_config / get_agent_spec / list_agent_specs (runtime registry)
- clear_agent_spec_cache
"""

from __future__ import annotations
import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from ypl.agent_harness_service.common.agent_registry import (
    _RUNTIME_CONFIGS,
    _db_agent_to_spec,
    _load_agent_spec,
    clear_agent_spec_cache,
    get_agent_spec,
    list_agent_specs,
    register_agent_config,
)
from ypl.agent_harness_service.common.models import AgentSpec, ExecutorConfig

# ===========================================================================
# Helpers
# ===========================================================================


def _make_config_file(tmp_path: Path, name: str, config: dict[str, Any]) -> str:
    agent_dir = tmp_path / name
    agent_dir.mkdir(exist_ok=True)
    config_path = agent_dir / "config.json"
    config_path.write_text(json.dumps(config))
    return str(config_path)


# ===========================================================================
# _load_agent_spec
# ===========================================================================


class TestLoadAgentSpec:
    def test_returns_none_without_executor_config(self, tmp_path: Path) -> None:
        path = _make_config_file(tmp_path, "no-exec", {"tool_permissions": {"*": "allow"}})
        result = _load_agent_spec("no-exec", path)
        assert result is None

    def test_loads_harnessed_spec(self, tmp_path: Path) -> None:
        path = _make_config_file(
            tmp_path,
            "basic",
            {
                "executor_config": {"type": "harnessed"},
                "tool_permissions": {"*": "allow"},
                "description": "A basic agent",
            },
        )
        result = _load_agent_spec("basic", path)
        assert result is not None
        assert result.name == "basic"
        assert result.executor.type == "harnessed"
        assert result.description == "A basic agent"

    def test_loads_raw_executor(self, tmp_path: Path) -> None:
        path = _make_config_file(
            tmp_path,
            "raw-agent",
            {
                "executor_config": {"type": "raw", "model": "anthropic/claude-sonnet-4-6"},
                "tool_permissions": {"*": "deny", "bash": "allow"},
            },
        )
        result = _load_agent_spec("raw-agent", path)
        assert result is not None
        assert result.executor.type == "raw"
        assert result.executor.model == "anthropic/claude-sonnet-4-6"

    def test_tool_permissions_expanded(self, tmp_path: Path) -> None:
        path = _make_config_file(
            tmp_path,
            "with-toolset",
            {
                "executor_config": {"type": "harnessed"},
                "tool_permissions": {"@readonly": "allow"},
            },
        )
        result = _load_agent_spec("with-toolset", path)
        assert result is not None
        assert "read" in result.tools
        assert "grep" in result.tools

    def test_returns_none_on_bad_json(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "bad"
        agent_dir.mkdir()
        path = str(agent_dir / "config.json")
        (agent_dir / "config.json").write_text("{{{ not json")
        result = _load_agent_spec("bad", path)
        assert result is None

    def test_returns_none_on_unknown_toolset(self, tmp_path: Path) -> None:
        path = _make_config_file(
            tmp_path,
            "bad-toolset",
            {
                "executor_config": {"type": "harnessed"},
                "tool_permissions": {"@unknown-toolset": "allow"},
            },
        )
        result = _load_agent_spec("bad-toolset", path)
        assert result is None

    def test_max_turns_loaded(self, tmp_path: Path) -> None:
        path = _make_config_file(
            tmp_path,
            "max-turns",
            {
                "executor_config": {"type": "harnessed"},
                "max_turns": 10,
            },
        )
        result = _load_agent_spec("max-turns", path)
        assert result is not None
        assert result.max_steps == 10

    def test_timeout_loaded(self, tmp_path: Path) -> None:
        path = _make_config_file(
            tmp_path,
            "timeout-agent",
            {
                "executor_config": {"type": "harnessed"},
                "timeout_s": 600,
            },
        )
        result = _load_agent_spec("timeout-agent", path)
        assert result is not None
        assert result.timeout_s == 600

    def test_allowed_subagents_loaded(self, tmp_path: Path) -> None:
        path = _make_config_file(
            tmp_path,
            "with-subs",
            {
                "executor_config": {"type": "harnessed"},
                "allowed_subagents": ["reviewer", "fixer"],
            },
        )
        result = _load_agent_spec("with-subs", path)
        assert result is not None
        assert "reviewer" in result.allowed_subagents

    def test_streaming_true_by_default(self, tmp_path: Path) -> None:
        path = _make_config_file(tmp_path, "streaming", {"executor_config": {"type": "harnessed"}})
        result = _load_agent_spec("streaming", path)
        assert result is not None
        assert result.streaming is True

    def test_history_bool_false_converted(self, tmp_path: Path) -> None:
        """executor_config.history can be a bool: false → {enabled: false}."""
        path = _make_config_file(
            tmp_path,
            "no-history",
            {"executor_config": {"type": "harnessed", "history": False}},
        )
        result = _load_agent_spec("no-history", path)
        assert result is not None
        assert result.executor.history.enabled is False

    def test_compaction_config_loaded(self, tmp_path: Path) -> None:
        path = _make_config_file(
            tmp_path,
            "compact",
            {
                "executor_config": {
                    "type": "harnessed",
                    "compaction": {
                        "enabled": False,
                        "target_ratio": 0.5,
                    },
                }
            },
        )
        result = _load_agent_spec("compact", path)
        assert result is not None
        assert result.executor.compaction.enabled is False
        assert result.executor.compaction.target_ratio == 0.5

    def test_required_tools_loaded(self, tmp_path: Path) -> None:
        tools = ["mcp__harness__send_slack_message"]
        path = _make_config_file(
            tmp_path,
            "tool-agent",
            {"executor_config": {"type": "harnessed"}, "required_tools": tools},
        )
        result = _load_agent_spec("tool-agent", path)
        assert result is not None
        assert result.required_tools == tools


# ===========================================================================
# _db_agent_to_spec
# ===========================================================================


class TestDbAgentToSpec:
    def test_returns_none_without_executor_config(self) -> None:
        result = _db_agent_to_spec("no-exec", {"tool_permissions": {"*": "allow"}})
        assert result is None

    def test_builds_spec_from_dict(self) -> None:
        result = _db_agent_to_spec(
            "db-agent",
            {
                "executor_config": {"type": "raw", "model": "anthropic/claude-sonnet-4-6"},
                "tool_permissions": {"*": "allow"},
            },
            description="A DB agent",
        )
        assert result is not None
        assert result.name == "db-agent"
        assert result.description == "A DB agent"
        assert result.executor.type == "raw"

    def test_toolset_expanded_from_db(self) -> None:
        result = _db_agent_to_spec(
            "db-readonly",
            {"executor_config": {"type": "harnessed"}, "tool_permissions": {"@readonly": "allow"}},
        )
        assert result is not None
        assert "read" in result.tools

    def test_returns_none_on_invalid_toolset(self) -> None:
        result = _db_agent_to_spec(
            "bad",
            {"executor_config": {"type": "harnessed"}, "tool_permissions": {"@nonexistent": "allow"}},
        )
        assert result is None

    def test_max_turns_from_config(self) -> None:
        result = _db_agent_to_spec(
            "limited",
            {"executor_config": {"type": "harnessed"}, "max_turns": 5},
        )
        assert result is not None
        assert result.max_steps == 5

    def test_history_bool_converted(self) -> None:
        result = _db_agent_to_spec(
            "no-history",
            {"executor_config": {"type": "harnessed", "history": False}},
        )
        assert result is not None
        assert result.executor.history.enabled is False

    def test_default_timeout(self) -> None:
        from ypl.agent_harness_service.common.constants import DEFAULT_TIMEOUT_S

        result = _db_agent_to_spec("default-timeout", {"executor_config": {"type": "harnessed"}})
        assert result is not None
        assert result.timeout_s == DEFAULT_TIMEOUT_S

    def test_required_tools_from_db(self) -> None:
        tools = ["mcp__harness__send_slack_message"]
        result = _db_agent_to_spec(
            "tool-agent",
            {"executor_config": {"type": "harnessed"}, "required_tools": tools},
        )
        assert result is not None
        assert result.required_tools == tools


# ===========================================================================
# Runtime registry: register / get / list
# ===========================================================================


class TestRuntimeRegistry:
    @pytest.fixture(autouse=True)
    def _clear_cache(self) -> Generator[None]:
        """Clear filesystem cache and runtime configs before each test to avoid cross-test pollution."""
        clear_agent_spec_cache()
        _RUNTIME_CONFIGS.clear()
        yield
        _RUNTIME_CONFIGS.clear()

    def _make_spec(self, name: str) -> AgentSpec:
        return AgentSpec(
            name=name,
            executor=ExecutorConfig(type="harnessed"),
        )

    def test_register_and_get_spec(self) -> None:
        spec = self._make_spec("test-registry-agent")
        register_agent_config(spec)
        result = get_agent_spec("test-registry-agent")
        assert result is not None
        assert result.name == "test-registry-agent"

    def test_get_nonexistent_returns_none(self) -> None:
        result = get_agent_spec("definitely-does-not-exist-xyz-12345")
        assert result is None

    def test_register_overwrites_existing(self) -> None:
        spec1 = AgentSpec(name="overwrite-test", executor=ExecutorConfig(type="harnessed"))
        spec2 = AgentSpec(name="overwrite-test", executor=ExecutorConfig(type="raw", model="anthropic/claude-haiku"))
        register_agent_config(spec1)
        register_agent_config(spec2)
        result = get_agent_spec("overwrite-test")
        assert result is not None
        assert result.executor.type == "raw"

    def test_list_includes_registered_specs(self) -> None:
        spec = self._make_spec("listed-agent-xyz")
        register_agent_config(spec)
        specs = list_agent_specs()
        names = [s.name for s in specs]
        assert "listed-agent-xyz" in names

    def test_list_returns_list_of_agent_specs(self) -> None:
        specs = list_agent_specs()
        assert isinstance(specs, list)
        for s in specs:
            assert isinstance(s, AgentSpec)


# ===========================================================================
# clear_agent_spec_cache
# ===========================================================================


class TestClearAgentSpecCache:
    def test_clear_does_not_raise(self) -> None:
        clear_agent_spec_cache()  # should not raise

    def test_clear_allows_fresh_discovery(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import ypl.agent_harness_service.common.agent_registry as _registry

        monkeypatch.setattr(_registry, "AHS_AGENTS_DIR", str(tmp_path))
        clear_agent_spec_cache()

        # Should gracefully handle empty directory
        specs = list_agent_specs()
        assert isinstance(specs, list)
