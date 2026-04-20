"""Tests for tool_permissions expansion, CLI flag derivation, and _build_args integration."""

import json
import os
import tempfile
from typing import Any
from unittest.mock import patch

import pytest
from ypl.agent_harness_service.common.config import (
    AgentConfig,
    SandboxConfig,
    clear_config_cache,
    load_agent_config,
)
from ypl.agent_harness_service.common.constants import EXECUTOR_TYPE_RAW, TOOLSETS
from ypl.agent_harness_service.common.models import (
    ExecutorConfig,
    ToolPermission,
    expand_tool_permissions,
    tool_permissions_to_cli_flags,
)
from ypl.agent_harness_service.common.types import SessionPermissions
from ypl.agent_harness_service.executors.runner import ClaudeCodeRunner, RunContext

# ---------------------------------------------------------------------------
# expand_tool_permissions
# ---------------------------------------------------------------------------


class TestExpandToolPermissions:
    def test_no_toolsets_passthrough(self) -> None:
        perms: dict[str, ToolPermission] = {"*": "deny", "read": "allow", "glob": "allow"}
        result = expand_tool_permissions(perms)
        assert result == {"*": "deny", "read": "allow", "glob": "allow"}

    def test_readonly_toolset_expansion(self) -> None:
        perms: dict[str, ToolPermission] = {"*": "deny", "@readonly": "allow"}
        result = expand_tool_permissions(perms)
        assert result["*"] == "deny"
        for tool in TOOLSETS["@readonly"]:
            assert result[tool] == "allow"
        assert "@readonly" not in result

    def test_readwrite_toolset_expansion(self) -> None:
        perms: dict[str, ToolPermission] = {"*": "deny", "@readwrite": "allow"}
        result = expand_tool_permissions(perms)
        for tool in TOOLSETS["@readwrite"]:
            assert result[tool] == "allow"
        assert "@readwrite" not in result

    def test_explicit_overrides_toolset(self) -> None:
        """Explicit per-tool entries take precedence over toolset expansion."""
        perms: dict[str, ToolPermission] = {
            "*": "deny",
            "@readonly": "allow",
            "read": "deny",  # explicit override
        }
        result = expand_tool_permissions(perms)
        # read should be "deny" (explicit), not "allow" (from @readonly)
        assert result["read"] == "deny"
        # Other @readonly tools should still be "allow"
        assert result["glob"] == "allow"
        assert result["grep"] == "allow"
        assert result["webfetch"] == "allow"

    def test_mixed_toolset_and_explicit(self) -> None:
        perms: dict[str, ToolPermission] = {
            "*": "deny",
            "@readonly": "allow",
            "route_model": "allow",
            "new_task": "allow",
        }
        result = expand_tool_permissions(perms)
        assert result["read"] == "allow"
        assert result["route_model"] == "allow"
        assert result["new_task"] == "allow"
        assert result["*"] == "deny"

    def test_unknown_toolset_raises(self) -> None:
        perms: dict[str, ToolPermission] = {"@nonexistent": "allow"}
        with pytest.raises(ValueError, match="Unknown toolset"):
            expand_tool_permissions(perms)

    def test_all_toolset_removed(self) -> None:
        """@all toolset no longer exists and should raise ValueError."""
        perms: dict[str, ToolPermission] = {"*": "deny", "@all": "allow"}
        with pytest.raises(ValueError, match="Unknown toolset"):
            expand_tool_permissions(perms)

    def test_empty_dict(self) -> None:
        result = expand_tool_permissions({})
        assert result == {}

    def test_wildcard_only(self) -> None:
        result = expand_tool_permissions({"*": "allow"})
        assert result == {"*": "allow"}


# ---------------------------------------------------------------------------
# tool_permissions_to_cli_flags
# ---------------------------------------------------------------------------


class TestToolPermissionsToCliFlags:
    def test_wildcard_deny_collects_allowed(self) -> None:
        """Generic lowercase names are mapped to PascalCase CLI names."""
        perms: dict[str, ToolPermission] = {"*": "deny", "read": "allow", "glob": "allow"}
        allowed, disallowed = tool_permissions_to_cli_flags(perms)
        assert allowed is not None
        assert set(allowed) == {"Read", "Glob"}
        assert disallowed is None

    def test_wildcard_allow_collects_denied(self) -> None:
        perms: dict[str, ToolPermission] = {"*": "allow", "edit": "deny", "write": "deny"}
        allowed, disallowed = tool_permissions_to_cli_flags(perms)
        assert allowed is None
        assert disallowed is not None
        assert set(disallowed) == {"Edit", "Write"}

    def test_wildcard_deny_no_allows(self) -> None:
        """With *:deny and no allow entries, returns empty allowed list."""
        perms: dict[str, ToolPermission] = {"*": "deny"}
        allowed, disallowed = tool_permissions_to_cli_flags(perms)
        assert allowed == []
        assert disallowed is None

    def test_wildcard_allow_no_denies(self) -> None:
        """With *:allow and no deny entries, returns (None, None)."""
        perms: dict[str, ToolPermission] = {"*": "allow"}
        allowed, disallowed = tool_permissions_to_cli_flags(perms)
        assert allowed is None
        assert disallowed is None

    def test_no_wildcard_no_denies(self) -> None:
        """No wildcard defaults to allow-all: explicit allows are ignored, no deny list."""
        perms: dict[str, ToolPermission] = {"read": "allow", "glob": "allow"}
        allowed, disallowed = tool_permissions_to_cli_flags(perms)
        assert allowed is None
        assert disallowed is None

    def test_empty_dict(self) -> None:
        allowed, disallowed = tool_permissions_to_cli_flags({})
        assert allowed is None
        assert disallowed is None

    def test_mcp_only_tools_skipped_in_cli_flags(self) -> None:
        """Tools without a CLI mapping (MCP-only like new_task) are excluded from CLI flags."""
        perms: dict[str, ToolPermission] = {
            "*": "deny",
            "read": "allow",
            "new_task": "allow",
            "route_model": "allow",
        }
        allowed, disallowed = tool_permissions_to_cli_flags(perms)
        assert allowed is not None
        assert set(allowed) == {"Read"}
        assert disallowed is None


# ---------------------------------------------------------------------------
# Integration: _build_args produces correct CLI flags
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> AgentConfig:
    defaults: dict[str, Any] = {
        "name": "test-agent",
        "config_dir": "/data/agents/test-agent",
        "executor_config": ExecutorConfig(type="harnessed"),
        "default_repo": "yupp-mind",
        "tool_permissions": {"*": "allow"},
        "sandbox": SandboxConfig(enabled=True, auto_allow_bash_if_sandboxed=True),
        "max_turns": 20,
        "max_budget_usd": 2.0,
        "has_mcp": False,
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_context(**overrides: Any) -> RunContext:
    defaults: dict[str, Any] = {
        "session_id": "test-session",
        "workspace": None,
        "llm_session_id": None,
    }
    defaults.update(overrides)
    return RunContext(**defaults)


class TestBuildArgsToolFlags:
    def _get_args(self, config: AgentConfig) -> list[str]:
        runner = ClaudeCodeRunner(config)
        with patch("ypl.agent_harness_service.executors.runner.build_system_prompt", return_value=""):
            return runner._build_args("test prompt", _make_context())

    def test_default_allow_no_flags(self) -> None:
        """Default *:allow with no MCP produces no --allowedTools/--disallowedTools."""
        config = _make_config(tool_permissions={"*": "allow"})
        args = self._get_args(config)
        assert "--allowedTools" not in args
        assert "--disallowedTools" not in args

    def test_deny_all_produces_allowed_tools(self) -> None:
        """*:deny with some allows produces --allowedTools with PascalCase CLI names."""
        config = _make_config(tool_permissions={"*": "deny", "read": "allow", "glob": "allow"})
        args = self._get_args(config)
        idx = args.index("--allowedTools")
        tools = args[idx + 1].split(",")
        assert "Read" in tools
        assert "Glob" in tools

    def test_allow_with_denies_produces_disallowed_tools(self) -> None:
        """*:allow with explicit denies produces --disallowedTools with PascalCase CLI names."""
        config = _make_config(tool_permissions={"*": "allow", "edit": "deny", "write": "deny"})
        args = self._get_args(config)
        idx = args.index("--disallowedTools")
        tools = args[idx + 1].split(",")
        assert "Edit" in tools
        assert "Write" in tools

    def test_deny_all_no_allows_uses_sentinel(self) -> None:
        """*:deny with no allows uses sentinel to ensure Claude CLI blocks all built-in tools."""
        config = _make_config(tool_permissions={"*": "deny"})
        args = self._get_args(config)
        idx = args.index("--allowedTools")
        tools = args[idx + 1].split(",")
        # Sentinel prevents empty --allowedTools "" which Claude CLI ignores
        assert tools == ["__none__"]

    def test_deny_all_with_mcp_access(self) -> None:
        """*:deny with MCP and full access appends MCP glob patterns to --allowedTools."""
        config = _make_config(
            tool_permissions={"*": "deny", "read": "allow"},
            has_mcp=True,
        )
        perms = SessionPermissions.full_access()
        ctx = _make_context(session_context={"permissions": perms.model_dump(mode="json")})
        runner = ClaudeCodeRunner(config)
        with patch("ypl.agent_harness_service.executors.runner.build_system_prompt", return_value=""):
            args = runner._build_args("test", ctx)
        idx = args.index("--allowedTools")
        tools = args[idx + 1].split(",")
        assert "Read" in tools
        assert "mcp__harness__*" in tools
        assert "mcp__yuppster-mcp-server__*" in tools

    def test_deny_all_with_mcp_restricted(self) -> None:
        """*:deny with MCP but restricted access appends only restricted harness tools."""
        config = _make_config(
            tool_permissions={"*": "deny", "read": "allow"},
            has_mcp=True,
        )
        perms = SessionPermissions.restricted()
        ctx = _make_context(session_context={"permissions": perms.model_dump(mode="json")})
        runner = ClaudeCodeRunner(config)
        with patch("ypl.agent_harness_service.executors.runner.build_system_prompt", return_value=""):
            args = runner._build_args("test", ctx)
        idx = args.index("--allowedTools")
        tools = args[idx + 1].split(",")
        assert "Read" in tools
        assert "mcp__harness__request_feedback" in tools
        assert "mcp__harness__*" not in tools


# ---------------------------------------------------------------------------
# load_agent_config: executor_config parsing
# ---------------------------------------------------------------------------


class TestLoadAgentConfigExecutor:
    """Verify load_agent_config correctly resolves executor from executor_config."""

    def _write_config(self, tmpdir: str, config: dict[str, Any]) -> str:
        agent_dir = os.path.join(tmpdir, "test-agent")
        os.makedirs(agent_dir, exist_ok=True)
        with open(os.path.join(agent_dir, "config.json"), "w") as f:
            json.dump(config, f)
        return "test-agent"

    def test_executor_config_raw(self) -> None:
        """executor_config.type=raw is parsed directly by Pydantic."""
        with tempfile.TemporaryDirectory() as tmpdir:
            name = self._write_config(
                tmpdir,
                {
                    "executor_config": {"type": "raw", "model": "anthropic/claude-sonnet-4-6"},
                    "tool_permissions": {"*": "deny"},
                },
            )
            with patch("ypl.agent_harness_service.common.config.AHS_AGENTS_DIR", tmpdir):
                clear_config_cache()
                cfg = load_agent_config(name)
            assert cfg is not None
            assert cfg.executor_config.type == EXECUTOR_TYPE_RAW
            assert cfg.executor_config.model == "anthropic/claude-sonnet-4-6"
            assert cfg.model == "anthropic/claude-sonnet-4-6"
            assert cfg.tool_permissions == {"*": "deny"}

    def test_no_executor_config_defaults_to_harnessed(self) -> None:
        """No executor_config defaults to harnessed with claude-code-cli."""
        with tempfile.TemporaryDirectory() as tmpdir:
            name = self._write_config(tmpdir, {})
            with patch("ypl.agent_harness_service.common.config.AHS_AGENTS_DIR", tmpdir):
                clear_config_cache()
                cfg = load_agent_config(name)
            assert cfg is not None
            assert cfg.executor_config.type == "harnessed"
            assert cfg.executor_config.model == "claude-code-cli"

    def test_executor_config_codex(self) -> None:
        """executor_config with codex-cli model is parsed correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            name = self._write_config(
                tmpdir,
                {"executor_config": {"type": "harnessed", "model": "codex-cli"}},
            )
            with patch("ypl.agent_harness_service.common.config.AHS_AGENTS_DIR", tmpdir):
                clear_config_cache()
                cfg = load_agent_config(name)
            assert cfg is not None
            assert cfg.executor_config.model == "codex-cli"
