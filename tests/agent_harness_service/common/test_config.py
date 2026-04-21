"""Tests for ypl.agent_harness_service.common.config.

Covers:
- validate_agent_name (valid/invalid patterns)
- read_file_if_exists (present/absent files)
- SandboxConfig defaults and custom values
- AgentConfig model property (harnessed vs raw, llm_model override)
- load_agent_config (from filesystem, JSON errors, missing file, legacy allowed_tools)
- load_agent_config_from_db (from DB agent record mock)
- clear_config_cache
"""

from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from ypl.agent_harness_service.common.config import (
    AgentConfig,
    SandboxConfig,
    clear_config_cache,
    load_agent_config,
    load_agent_config_from_db,
    read_file_if_exists,
    validate_agent_name,
)
from ypl.agent_harness_service.common.models import ExecutorConfig

# ===========================================================================
# validate_agent_name
# ===========================================================================


class TestValidateAgentName:
    def test_valid_simple_name(self) -> None:
        validate_agent_name("sre")  # should not raise

    def test_valid_hyphenated_name(self) -> None:
        validate_agent_name("code-reviewer")  # should not raise

    def test_valid_alphanumeric(self) -> None:
        validate_agent_name("agent123")  # should not raise

    def test_valid_starts_with_digit(self) -> None:
        validate_agent_name("1agent")  # should not raise

    def test_invalid_uppercase(self) -> None:
        with pytest.raises(ValueError, match="Invalid agent name"):
            validate_agent_name("SRE")

    def test_invalid_path_traversal(self) -> None:
        with pytest.raises(ValueError, match="Invalid agent name"):
            validate_agent_name("../etc/passwd")

    def test_invalid_spaces(self) -> None:
        with pytest.raises(ValueError, match="Invalid agent name"):
            validate_agent_name("my agent")

    def test_invalid_underscore(self) -> None:
        with pytest.raises(ValueError, match="Invalid agent name"):
            validate_agent_name("my_agent")

    def test_invalid_empty_string(self) -> None:
        with pytest.raises(ValueError, match="Invalid agent name"):
            validate_agent_name("")

    def test_invalid_dot(self) -> None:
        with pytest.raises(ValueError, match="Invalid agent name"):
            validate_agent_name("agent.name")

    def test_invalid_leading_hyphen(self) -> None:
        with pytest.raises(ValueError, match="Invalid agent name"):
            validate_agent_name("-agent")


# ===========================================================================
# read_file_if_exists
# ===========================================================================


class TestReadFileIfExists:
    def test_reads_existing_file(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world")
            f.flush()
            result = read_file_if_exists(f.name)
        os.unlink(f.name)
        assert result == "hello world"

    def test_returns_none_for_missing_file(self) -> None:
        result = read_file_if_exists("/nonexistent/path/to/file.txt")
        assert result is None

    def test_returns_empty_string_for_empty_file(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.flush()
            result = read_file_if_exists(f.name)
        os.unlink(f.name)
        assert result == ""


# ===========================================================================
# SandboxConfig
# ===========================================================================


class TestSandboxConfig:
    def test_defaults(self) -> None:
        cfg = SandboxConfig()
        assert cfg.enabled is True
        assert cfg.auto_allow_bash_if_sandboxed is True
        assert cfg.bwrap_enabled is True

    def test_disabled_sandbox(self) -> None:
        cfg = SandboxConfig(enabled=False, auto_allow_bash_if_sandboxed=False, bwrap_enabled=False)
        assert cfg.enabled is False
        assert cfg.auto_allow_bash_if_sandboxed is False
        assert cfg.bwrap_enabled is False


# ===========================================================================
# AgentConfig.model property
# ===========================================================================


class TestAgentConfigModelProperty:
    def _make_config(self, **kwargs: Any) -> AgentConfig:
        return AgentConfig(name="test-agent", config_dir="/tmp/test-agent", **kwargs)

    def test_harnessed_no_llm_model_returns_empty(self) -> None:
        cfg = self._make_config(executor_config=ExecutorConfig(type="harnessed"))
        assert cfg.model == ""

    def test_harnessed_with_llm_model_override(self) -> None:
        cfg = self._make_config(
            executor_config=ExecutorConfig(type="harnessed"),
            llm_model="anthropic/claude-opus-4",
        )
        assert cfg.model == "anthropic/claude-opus-4"

    def test_raw_executor_returns_model(self) -> None:
        cfg = self._make_config(
            executor_config=ExecutorConfig(type="raw", model="anthropic/claude-sonnet-4-6"),
        )
        assert cfg.model == "anthropic/claude-sonnet-4-6"

    def test_raw_executor_no_model_returns_empty(self) -> None:
        cfg = self._make_config(executor_config=ExecutorConfig(type="raw", model=None))
        assert cfg.model == ""

    def test_llm_model_takes_precedence_over_raw(self) -> None:
        """llm_model override should take precedence even for raw executors."""
        cfg = self._make_config(
            executor_config=ExecutorConfig(type="raw", model="anthropic/claude-sonnet-4-6"),
            llm_model="anthropic/claude-haiku-4-5",
        )
        assert cfg.model == "anthropic/claude-haiku-4-5"


# ===========================================================================
# load_agent_config (filesystem)
# ===========================================================================


class TestLoadAgentConfig:
    def _write_config(self, agent_dir: Path, config: dict[str, Any]) -> None:
        (agent_dir / "config.json").write_text(json.dumps(config))

    def test_loads_minimal_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_dir = tmp_path / "my-agent"
        agent_dir.mkdir()
        self._write_config(
            agent_dir,
            {
                "display_name": "My Agent",
                "executor_config": {"type": "harnessed"},
                "tool_permissions": {"*": "allow"},
            },
        )
        monkeypatch.setenv("AHS_AGENTS_DIR", str(tmp_path))
        # Reload constants so AHS_AGENTS_DIR is picked up
        import ypl.agent_harness_service.common.constants as _constants

        monkeypatch.setattr(_constants, "AHS_AGENTS_DIR", str(tmp_path))
        import ypl.agent_harness_service.common.config as _config

        monkeypatch.setattr(_config, "AHS_AGENTS_DIR", str(tmp_path))
        clear_config_cache()

        result = load_agent_config("my-agent")
        assert result is not None
        assert result.name == "my-agent"
        assert result.display_name == "My Agent"

    def test_returns_none_when_config_json_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_dir = tmp_path / "no-config"
        agent_dir.mkdir()
        import ypl.agent_harness_service.common.config as _config

        monkeypatch.setattr(_config, "AHS_AGENTS_DIR", str(tmp_path))
        clear_config_cache()

        result = load_agent_config("no-config")
        assert result is None

    def test_returns_none_on_invalid_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_dir = tmp_path / "bad-json"
        agent_dir.mkdir()
        (agent_dir / "config.json").write_text("not valid json {{{")
        import ypl.agent_harness_service.common.config as _config

        monkeypatch.setattr(_config, "AHS_AGENTS_DIR", str(tmp_path))
        clear_config_cache()

        result = load_agent_config("bad-json")
        assert result is None

    def test_invalid_agent_name_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid agent name"):
            load_agent_config("INVALID_NAME")

    def test_returns_none_on_unknown_toolset(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_dir = tmp_path / "bad-perms"
        agent_dir.mkdir()
        self._write_config(agent_dir, {"tool_permissions": {"@nonexistent": "allow"}})
        import ypl.agent_harness_service.common.config as _config

        monkeypatch.setattr(_config, "AHS_AGENTS_DIR", str(tmp_path))
        clear_config_cache()

        result = load_agent_config("bad-perms")
        assert result is None

    def test_sandbox_config_parsed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_dir = tmp_path / "sandboxed"
        agent_dir.mkdir()
        self._write_config(
            agent_dir,
            {
                "sandbox": {
                    "enabled": False,
                    "autoAllowBashIfSandboxed": False,
                    "bwrapEnabled": False,
                },
                "tool_permissions": {"*": "allow"},
            },
        )
        import ypl.agent_harness_service.common.config as _config

        monkeypatch.setattr(_config, "AHS_AGENTS_DIR", str(tmp_path))
        clear_config_cache()

        result = load_agent_config("sandboxed")
        assert result is not None
        assert result.sandbox.enabled is False
        assert result.sandbox.auto_allow_bash_if_sandboxed is False
        assert result.sandbox.bwrap_enabled is False

    def test_max_turns_loaded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_dir = tmp_path / "limited"
        agent_dir.mkdir()
        self._write_config(agent_dir, {"max_turns": 5, "tool_permissions": {"*": "allow"}})
        import ypl.agent_harness_service.common.config as _config

        monkeypatch.setattr(_config, "AHS_AGENTS_DIR", str(tmp_path))
        clear_config_cache()

        result = load_agent_config("limited")
        assert result is not None
        assert result.max_turns == 5

    def test_allowed_gateways_loaded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_dir = tmp_path / "slack-only"
        agent_dir.mkdir()
        self._write_config(
            agent_dir,
            {"allowed_gateways": ["slack"], "tool_permissions": {"*": "allow"}},
        )
        import ypl.agent_harness_service.common.config as _config

        monkeypatch.setattr(_config, "AHS_AGENTS_DIR", str(tmp_path))
        clear_config_cache()

        result = load_agent_config("slack-only")
        assert result is not None
        assert result.allowed_gateways == ["slack"]

    def test_required_tools_loaded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_dir = tmp_path / "tool-agent"
        agent_dir.mkdir()
        tools = ["mcp__harness__send_slack_message", "mcp__agcouch-mcp-server__query_agentdb"]
        self._write_config(agent_dir, {"required_tools": tools, "tool_permissions": {"*": "allow"}})
        import ypl.agent_harness_service.common.config as _config

        monkeypatch.setattr(_config, "AHS_AGENTS_DIR", str(tmp_path))
        clear_config_cache()

        result = load_agent_config("tool-agent")
        assert result is not None
        assert result.required_tools == tools

    def test_legacy_allowed_tools_converted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_dir = tmp_path / "legacy"
        agent_dir.mkdir()
        self._write_config(agent_dir, {"allowed_tools": ["Bash", "Read"]})
        import ypl.agent_harness_service.common.config as _config

        monkeypatch.setattr(_config, "AHS_AGENTS_DIR", str(tmp_path))
        clear_config_cache()

        result = load_agent_config("legacy")
        assert result is not None
        # Legacy allowed_tools → default-deny with explicit allows
        assert result.tool_permissions.get("*") == "deny"

    def test_description_loaded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_dir = tmp_path / "described"
        agent_dir.mkdir()
        self._write_config(
            agent_dir,
            {"description": "A described agent", "tool_permissions": {"*": "allow"}},
        )
        import ypl.agent_harness_service.common.config as _config

        monkeypatch.setattr(_config, "AHS_AGENTS_DIR", str(tmp_path))
        clear_config_cache()

        result = load_agent_config("described")
        assert result is not None
        assert result.description == "A described agent"

    def test_default_repo_loaded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_dir = tmp_path / "repo-agent"
        agent_dir.mkdir()
        self._write_config(
            agent_dir,
            {"default_repo": "yupp-mind", "tool_permissions": {"*": "allow"}},
        )
        import ypl.agent_harness_service.common.config as _config

        monkeypatch.setattr(_config, "AHS_AGENTS_DIR", str(tmp_path))
        clear_config_cache()

        result = load_agent_config("repo-agent")
        assert result is not None
        assert result.default_repo == "yupp-mind"

    def test_tool_permissions_default_when_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_dir = tmp_path / "default-perms"
        agent_dir.mkdir()
        # No tool_permissions key, no allowed_tools — should default to allow all
        self._write_config(agent_dir, {"max_turns": 10})
        import ypl.agent_harness_service.common.config as _config

        monkeypatch.setattr(_config, "AHS_AGENTS_DIR", str(tmp_path))
        clear_config_cache()

        result = load_agent_config("default-perms")
        assert result is not None
        assert result.tool_permissions == {"*": "allow"}


# ===========================================================================
# load_agent_config_from_db
# ===========================================================================


class TestLoadAgentConfigFromDb:
    def _make_db_agent(self, **kwargs: Any) -> MagicMock:
        agent = MagicMock()
        agent.name = kwargs.get("name", "db-agent")
        agent.display_name = kwargs.get("display_name", "DB Agent")
        agent.description = kwargs.get("description", None)
        agent.config = kwargs.get("config", {})
        agent.additional_system_prompt = kwargs.get("additional_system_prompt", None)
        return agent

    def test_minimal_db_agent(self) -> None:
        agent = self._make_db_agent(config={})
        result = load_agent_config_from_db(agent)
        assert result.name == "db-agent"
        assert result.display_name == "DB Agent"
        assert result.tool_permissions == {"*": "allow"}

    def test_tool_permissions_from_db(self) -> None:
        agent = self._make_db_agent(config={"tool_permissions": {"*": "deny", "bash": "allow"}})
        result = load_agent_config_from_db(agent)
        assert result.tool_permissions["*"] == "deny"
        assert result.tool_permissions["bash"] == "allow"

    def test_sandbox_config_from_db(self) -> None:
        agent = self._make_db_agent(
            config={
                "sandbox": {
                    "enabled": False,
                    "autoAllowBashIfSandboxed": False,
                    "bwrapEnabled": False,
                }
            }
        )
        result = load_agent_config_from_db(agent)
        assert result.sandbox.enabled is False

    def test_additional_system_prompt(self) -> None:
        agent = self._make_db_agent(additional_system_prompt="You are a DB agent.")
        result = load_agent_config_from_db(agent)
        assert result.additional_system_prompt == "You are a DB agent."

    def test_max_turns_from_db(self) -> None:
        agent = self._make_db_agent(config={"max_turns": 100})
        result = load_agent_config_from_db(agent)
        assert result.max_turns == 100

    def test_has_mcp_defaults_true_for_db_agents(self) -> None:
        agent = self._make_db_agent(config={})
        result = load_agent_config_from_db(agent)
        assert result.has_mcp is True

    def test_display_name_falls_back_to_name(self) -> None:
        agent = self._make_db_agent(name="yuppclaw-alice", display_name=None)
        result = load_agent_config_from_db(agent)
        assert result.display_name == "yuppclaw-alice"

    def test_allowed_subagents_from_db(self) -> None:
        agent = self._make_db_agent(config={"allowed_subagents": ["reviewer", "fixer"]})
        result = load_agent_config_from_db(agent)
        assert result.allowed_subagents == ["reviewer", "fixer"]

    def test_required_tools_from_db(self) -> None:
        tools = ["mcp__harness__send_slack_message"]
        agent = self._make_db_agent(config={"required_tools": tools})
        result = load_agent_config_from_db(agent)
        assert result.required_tools == tools

    def test_executor_config_from_db(self) -> None:
        agent = self._make_db_agent(
            config={
                "executor_config": {"type": "raw", "model": "anthropic/claude-sonnet-4-6"},
            }
        )
        result = load_agent_config_from_db(agent)
        assert result.executor_config.type == "raw"
        assert result.executor_config.model == "anthropic/claude-sonnet-4-6"

    def test_toolset_expansion_from_db(self) -> None:
        agent = self._make_db_agent(config={"tool_permissions": {"@readonly": "allow"}})
        result = load_agent_config_from_db(agent)
        assert "read" in result.tool_permissions
        assert "grep" in result.tool_permissions


# ===========================================================================
# clear_config_cache
# ===========================================================================


class TestClearConfigCache:
    def test_clear_does_not_raise(self) -> None:
        clear_config_cache()  # should not raise

    def test_after_clear_fresh_load_works(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_dir = tmp_path / "cached-agent"
        agent_dir.mkdir()
        (agent_dir / "config.json").write_text(json.dumps({"tool_permissions": {"*": "allow"}}))
        import ypl.agent_harness_service.common.config as _config

        monkeypatch.setattr(_config, "AHS_AGENTS_DIR", str(tmp_path))
        clear_config_cache()

        r1 = load_agent_config("cached-agent")
        clear_config_cache()
        r2 = load_agent_config("cached-agent")
        assert r1 is not None
        assert r2 is not None
        assert r1.name == r2.name
