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
        "config_dir": "/data/ahs/agents/test-agent",
        "executor_config": ExecutorConfig(type="harnessed"),
        "default_repo": "yupp-agent",
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
        """Default *:allow with no MCP still emits --disallowedTools for the always-denied list.

        ``AskUserQuestion`` (and any future built-in that can't work in headless
        ``claude -p`` mode) is always denied regardless of session shape, so the
        deny flag is non-empty even in the otherwise-empty default-allow path.
        ``--allowedTools`` remains absent.
        """
        config = _make_config(tool_permissions={"*": "allow"})
        args = self._get_args(config)
        assert "--allowedTools" not in args
        idx = args.index("--disallowedTools")
        denied = args[idx + 1].split(",")
        assert "AskUserQuestion" in denied

    def test_always_disallowed_tools_present_in_all_modes(self) -> None:
        """``AskUserQuestion`` is denied in every tool-permission shape.

        Headless ``claude -p`` mode (which is how AHS always invokes the CLI)
        has no UI to render the multi-option question dialog, so attempts to
        call this tool return a cryptic ``"Answer questions?"`` error that
        looks indistinguishable from session hangs.  Pin the deny so the model
        never sees the tool as available.
        """
        # Denylist mode with explicit denies.
        denylist_config = _make_config(tool_permissions={"*": "allow", "edit": "deny"})
        denylist_args = self._get_args(denylist_config)
        idx = denylist_args.index("--disallowedTools")
        assert "AskUserQuestion" in denylist_args[idx + 1].split(",")

        # Default-allow mode with MCP off (covered by test_default_allow_no_flags too,
        # but pinned here as part of the umbrella assertion).
        default_allow_config = _make_config(tool_permissions={"*": "allow"})
        default_allow_args = self._get_args(default_allow_config)
        idx = default_allow_args.index("--disallowedTools")
        assert "AskUserQuestion" in default_allow_args[idx + 1].split(",")

        # Allowlist mode (``*: deny``) — even though anything not in the allowed
        # list is implicitly denied, the always-disallowed list still surfaces
        # explicitly in ``--disallowedTools`` so the intent is auditable in
        # the executor command line.
        allowlist_config = _make_config(tool_permissions={"*": "deny", "read": "allow"})
        allowlist_args = self._get_args(allowlist_config)
        idx = allowlist_args.index("--disallowedTools")
        assert "AskUserQuestion" in allowlist_args[idx + 1].split(",")

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
        """``*:deny`` with MCP and full access appends harness glob to --allowedTools.

        After phase-2 (PR #300) every shared / external-data tool that
        used to live on platform is reachable via ``mcp__harness__*``, so
        the platform wildcard is gone — full access is described entirely
        by the harness glob plus the eager-load predeclares.
        """
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
        # Regression guard: the dead platform wildcard never reappears.
        assert "mcp__platform-mcp-server__*" not in tools
        # Eager-load fast path: top shared tools are explicitly named so
        # Claude Code skips the deferred-discovery ToolSearch round-trip.
        assert "mcp__harness__query_appdb" in tools
        assert "mcp__harness__search_gcp_logs" in tools
        assert "mcp__harness__add_artifact" in tools
        assert "mcp__harness__report_security_incident" in tools

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

    def test_default_allow_restricted_blocks_migrated_shared_tools(self) -> None:
        """Restricted sessions in default-allow mode cannot reach the migrated shared tools.

        Pre phase-2 these were on the platform mount and blocked for
        restricted sessions via the ``mcp__platform-mcp-server__*``
        wildcard. After PR #300 they live under ``mcp__harness__*``;
        the deny list enumerates them so the security boundary is
        preserved.
        """
        config = _make_config(
            tool_permissions={"*": "allow"},  # default-allow mode
            has_mcp=True,
        )
        perms = SessionPermissions.restricted()
        ctx = _make_context(session_context={"permissions": perms.model_dump(mode="json")})
        runner = ClaudeCodeRunner(config)
        with patch("ypl.agent_harness_service.executors.runner.build_system_prompt", return_value=""):
            args = runner._build_args("test", ctx)
        idx = args.index("--disallowedTools")
        denied = args[idx + 1].split(",")
        # Every shared/external-data tool migrated by PR #300 is denied.
        for blocked in (
            "mcp__harness__query_appdb",
            "mcp__harness__query_bigquery_expensive",
            "mcp__harness__search_gcp_logs",
            "mcp__harness__get_sentry_issue_details",
            "mcp__harness__search_twitter",
            "mcp__harness__read_slack_thread",
            "mcp__harness__save_memory",
        ):
            assert blocked in denied, f"{blocked} should be in restricted deny list"
        # Regression guard: the dead platform wildcard never reappears.
        assert "mcp__platform-mcp-server__*" not in denied
        # ``report_security_incident`` is intentionally *not* blocked —
        # SECURITY.md instructs every agent to call it.
        assert "mcp__harness__report_security_incident" not in denied


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
