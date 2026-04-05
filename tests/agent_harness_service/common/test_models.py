"""Tests for ypl.agent_harness_service.common.models.

Covers:
- expand_tool_permissions (toolset expansion, explicit overrides, error handling)
- tool_permissions_to_cli_flags (default-allow vs default-deny, mapping)
- ExecutorConfig (defaults, model_validator, llm_model property)
- CompactionConfig, HistoryConfig, RetryConfig
- AgentSpec defaults
- SubagentSession lifecycle
"""

from __future__ import annotations
import time

import pytest
from pydantic import ValidationError
from ypl.agent_harness_service.common.models import (
    AgentSpec,
    CompactionConfig,
    ExecutorConfig,
    HistoryConfig,
    RetryConfig,
    SubagentSession,
    ToolPermission,
    _default_tools,
    expand_tool_permissions,
    tool_permissions_to_cli_flags,
)

# ===========================================================================
# _default_tools
# ===========================================================================


class TestDefaultTools:
    def test_returns_allow_wildcard(self) -> None:
        result = _default_tools()
        assert result == {"*": "allow"}

    def test_returns_new_dict_each_call(self) -> None:
        a = _default_tools()
        b = _default_tools()
        a["extra"] = "deny"
        assert "extra" not in b


# ===========================================================================
# expand_tool_permissions
# ===========================================================================


class TestExpandToolPermissions:
    def test_empty_dict_returns_empty(self) -> None:
        assert expand_tool_permissions({}) == {}

    def test_wildcard_passthrough(self) -> None:
        result = expand_tool_permissions({"*": "allow"})
        assert result == {"*": "allow"}

    def test_readonly_toolset_expansion(self) -> None:
        result = expand_tool_permissions({"@readonly": "allow"})
        assert "read" in result
        assert "glob" in result
        assert "grep" in result
        assert "webfetch" in result
        assert "websearch" in result
        assert all(v == "allow" for v in result.values())

    def test_readwrite_toolset_expansion(self) -> None:
        result = expand_tool_permissions({"@readwrite": "deny"})
        assert "bash" in result
        assert "write" in result
        assert "edit" in result
        assert all(v == "deny" for v in result.values())

    def test_explicit_overrides_toolset(self) -> None:
        """Explicit per-tool entry takes precedence over toolset-expanded entry."""
        result = expand_tool_permissions({"@readonly": "allow", "grep": "deny"})
        # grep was in @readonly (allow) but explicit deny wins
        assert result["grep"] == "deny"
        assert result["read"] == "allow"

    def test_unknown_toolset_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown toolset"):
            expand_tool_permissions({"@nonexistent": "allow"})

    def test_mixed_toolset_and_explicit(self) -> None:
        result = expand_tool_permissions({"*": "deny", "@readonly": "allow", "bash": "allow"})
        assert result["*"] == "deny"
        assert result["bash"] == "allow"
        assert result["read"] == "allow"

    def test_deny_permission_preserved(self) -> None:
        result = expand_tool_permissions({"@readwrite": "deny"})
        assert result["edit"] == "deny"

    def test_ask_permission_preserved(self) -> None:
        result = expand_tool_permissions({"bash": "ask"})
        assert result["bash"] == "ask"


# ===========================================================================
# tool_permissions_to_cli_flags
# ===========================================================================


class TestToolPermissionsToCliFlags:
    def test_default_allow_no_denied_tools_returns_none_none(self) -> None:
        allowed, disallowed = tool_permissions_to_cli_flags({"*": "allow"})
        assert allowed is None
        assert disallowed is None

    def test_default_allow_with_denied_tools(self) -> None:
        allowed, disallowed = tool_permissions_to_cli_flags({"*": "allow", "bash": "deny"})
        assert allowed is None
        assert disallowed is not None
        assert "Bash" in disallowed

    def test_default_deny_no_allowed_tools_returns_empty_list(self) -> None:
        allowed, disallowed = tool_permissions_to_cli_flags({"*": "deny"})
        assert allowed == []
        assert disallowed is None

    def test_default_deny_with_allowed_tools(self) -> None:
        allowed, disallowed = tool_permissions_to_cli_flags({"*": "deny", "read": "allow", "bash": "allow"})
        assert allowed is not None
        assert "Read" in allowed
        assert "Bash" in allowed
        assert disallowed is None

    def test_tools_without_cli_mapping_are_skipped(self) -> None:
        """MCP-only tools like new_task don't appear in CLI flags."""
        allowed, disallowed = tool_permissions_to_cli_flags({"*": "deny", "new_task": "allow"})
        # new_task is not in HARNESS_TO_CLI_TOOL_MAP
        assert allowed is not None
        assert "new_task" not in allowed

    def test_ask_in_default_deny_not_in_allowed(self) -> None:
        """In default-deny mode, 'ask' tools are not in the allowed list."""
        allowed, disallowed = tool_permissions_to_cli_flags({"*": "deny", "bash": "ask"})
        assert allowed is not None
        assert "Bash" not in allowed

    def test_ask_in_default_allow_not_in_disallowed(self) -> None:
        """In default-allow mode, 'ask' tools are effectively allowed — no disallowed list."""
        allowed, disallowed = tool_permissions_to_cli_flags({"*": "allow", "bash": "ask"})
        # No deny entries → disallowed is None (ask is treated as allowed)
        assert allowed is None
        assert disallowed is None

    def test_all_cli_tools_can_be_denied(self) -> None:
        perms: dict[str, ToolPermission] = {
            "*": "allow",
            "bash": "deny",
            "read": "deny",
            "write": "deny",
            "edit": "deny",
        }
        allowed, disallowed = tool_permissions_to_cli_flags(perms)
        assert disallowed is not None
        assert "Bash" in disallowed
        assert "Read" in disallowed

    def test_no_wildcard_treated_as_default_allow(self) -> None:
        """Missing wildcard = default allow."""
        allowed, disallowed = tool_permissions_to_cli_flags({"bash": "deny"})
        assert allowed is None
        assert disallowed is not None
        assert "Bash" in disallowed


# ===========================================================================
# CompactionConfig
# ===========================================================================


class TestCompactionConfig:
    def test_defaults(self) -> None:
        cfg = CompactionConfig()
        assert cfg.enabled is True
        assert cfg.model == "anthropic/claude-haiku-4-5"
        assert cfg.prune_protect_steps == 2
        assert cfg.target_ratio == 0.7

    def test_custom_values(self) -> None:
        cfg = CompactionConfig(enabled=False, target_ratio=0.5, prune_protect_steps=5)
        assert cfg.enabled is False
        assert cfg.target_ratio == 0.5
        assert cfg.prune_protect_steps == 5


# ===========================================================================
# HistoryConfig
# ===========================================================================


class TestHistoryConfig:
    def test_default_enabled(self) -> None:
        cfg = HistoryConfig()
        assert cfg.enabled is True

    def test_disabled(self) -> None:
        cfg = HistoryConfig(enabled=False)
        assert cfg.enabled is False


# ===========================================================================
# RetryConfig
# ===========================================================================


class TestRetryConfig:
    def test_defaults(self) -> None:
        cfg = RetryConfig()
        assert cfg.max_retries == 1
        assert cfg.on_empty_result is True

    def test_disabled(self) -> None:
        cfg = RetryConfig(max_retries=0, on_empty_result=False)
        assert cfg.max_retries == 0
        assert cfg.on_empty_result is False


# ===========================================================================
# ExecutorConfig
# ===========================================================================


class TestExecutorConfig:
    def test_default_type_is_harnessed(self) -> None:
        cfg = ExecutorConfig()
        assert cfg.type == "harnessed"

    def test_harnessed_default_model_is_claude_code_cli(self) -> None:
        """model_validator sets model='claude-code-cli' for harnessed type when None."""
        cfg = ExecutorConfig(type="harnessed")
        assert cfg.model == "claude-code-cli"

    def test_harnessed_explicit_model_preserved(self) -> None:
        cfg = ExecutorConfig(type="harnessed", model="codex-cli")
        assert cfg.model == "codex-cli"

    def test_raw_type_no_default_model(self) -> None:
        cfg = ExecutorConfig(type="raw", model="anthropic/claude-sonnet-4-6")
        assert cfg.model == "anthropic/claude-sonnet-4-6"

    def test_raw_type_llm_model_property(self) -> None:
        cfg = ExecutorConfig(type="raw", model="anthropic/claude-haiku-4-5")
        assert cfg.llm_model == "anthropic/claude-haiku-4-5"

    def test_harnessed_llm_model_property_is_none(self) -> None:
        cfg = ExecutorConfig(type="harnessed")
        assert cfg.llm_model is None

    def test_invalid_type_raises(self) -> None:
        with pytest.raises(ValidationError):
            ExecutorConfig(type="invalid")

    def test_default_tool_result_limits(self) -> None:
        cfg = ExecutorConfig()
        assert cfg.max_tool_result_chars == 30_000
        assert cfg.max_tool_result_lines == 1_000
        assert cfg.max_tool_result_spill_chars == 100_000

    def test_custom_limits(self) -> None:
        cfg = ExecutorConfig(max_tool_result_chars=5000, max_tool_result_lines=100)
        assert cfg.max_tool_result_chars == 5000
        assert cfg.max_tool_result_lines == 100

    def test_nested_compaction_defaults(self) -> None:
        cfg = ExecutorConfig()
        assert cfg.compaction.enabled is True

    def test_nested_retry_defaults(self) -> None:
        cfg = ExecutorConfig()
        assert cfg.retry.max_retries == 1


# ===========================================================================
# AgentSpec
# ===========================================================================


class TestAgentSpec:
    def test_minimal_creation(self) -> None:
        spec = AgentSpec(name="test-agent")
        assert spec.name == "test-agent"
        assert spec.description is None
        assert spec.streaming is True

    def test_default_tools_allow_all(self) -> None:
        spec = AgentSpec(name="test")
        assert spec.tools == {"*": "allow"}

    def test_default_executor_config(self) -> None:
        spec = AgentSpec(name="test")
        assert spec.executor.type == "harnessed"

    def test_custom_tools(self) -> None:
        tools: dict[str, ToolPermission] = {"*": "deny", "bash": "allow"}
        spec = AgentSpec(name="test", tools=tools)
        assert spec.tools["bash"] == "allow"
        assert spec.tools["*"] == "deny"

    def test_allowed_subagents(self) -> None:
        spec = AgentSpec(name="test", allowed_subagents=["reviewer", "fixer"])
        assert "reviewer" in spec.allowed_subagents

    def test_additional_system_prompt(self) -> None:
        spec = AgentSpec(name="test", additional_system_prompt="Extra context here.")
        assert spec.additional_system_prompt == "Extra context here."

    def test_required_tools_default_empty(self) -> None:
        spec = AgentSpec(name="test")
        assert spec.required_tools == []

    def test_temperature_none_by_default(self) -> None:
        spec = AgentSpec(name="test")
        assert spec.temperature is None

    def test_model_parameters_none_by_default(self) -> None:
        spec = AgentSpec(name="test")
        assert spec.model_parameters is None


# ===========================================================================
# SubagentSession
# ===========================================================================


class TestSubagentSession:
    def test_default_status_is_running(self) -> None:
        session = SubagentSession(
            parent_session_id="parent-123",
            agent_name="reviewer",
            model="anthropic/claude-sonnet-4-6",
            executor_type="raw",
        )
        assert session.status == "running"

    def test_id_is_uuid_string(self) -> None:
        session = SubagentSession(
            parent_session_id="parent-123",
            agent_name="reviewer",
            model="anthropic/claude-sonnet-4-6",
            executor_type="raw",
        )
        import uuid

        uuid.UUID(session.id)  # should not raise

    def test_unique_ids(self) -> None:
        ids = {
            SubagentSession(
                parent_session_id="p",
                agent_name="a",
                model="m",
                executor_type="raw",
            ).id
            for _ in range(10)
        }
        assert len(ids) == 10

    def test_time_created_is_recent(self) -> None:
        before = time.time()
        session = SubagentSession(
            parent_session_id="parent-123",
            agent_name="reviewer",
            model="anthropic/claude-sonnet-4-6",
            executor_type="raw",
        )
        after = time.time()
        assert before <= session.time_created <= after

    def test_result_is_none_by_default(self) -> None:
        session = SubagentSession(
            parent_session_id="parent-123",
            agent_name="reviewer",
            model="anthropic/claude-sonnet-4-6",
            executor_type="raw",
        )
        assert session.result is None
        assert session.time_completed is None

    def test_valid_status_values(self) -> None:
        for status in ("running", "completed", "error"):
            session = SubagentSession(
                parent_session_id="p",
                agent_name="a",
                model="m",
                executor_type="raw",
                status=status,
            )
            assert session.status == status

    def test_invalid_status_raises(self) -> None:
        with pytest.raises(ValidationError):
            SubagentSession(
                parent_session_id="p",
                agent_name="a",
                model="m",
                executor_type="raw",
                status="unknown",
            )
