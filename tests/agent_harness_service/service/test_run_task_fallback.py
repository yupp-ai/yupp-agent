"""Tests for the rate-limit fallback helpers in run_task."""

from ypl.agent_harness_service.common.config import AgentConfig
from ypl.agent_harness_service.common.models import ExecutorConfig
from ypl.agent_harness_service.service.run_task import (
    _apply_chain_entry,
    _collect_failure_text,
    _config_to_canonical,
)


def _harnessed_cfg() -> AgentConfig:
    return AgentConfig(
        name="agent",
        config_dir="/tmp/agent",
        executor_config=ExecutorConfig(type="harnessed", model="claude-code-cli"),
    )


def test_apply_chain_entry_to_raw() -> None:
    out = _apply_chain_entry(_harnessed_cfg(), "raw:deepseek/deepseek-chat")
    assert out.executor_config.type == "raw"
    assert out.executor_config.model == "deepseek/deepseek-chat"
    assert out.executor_config.harness is None
    assert out.llm_model is None
    # AgentConfig.model returns the raw provider/model for raw executors.
    assert out.model == "deepseek/deepseek-chat"


def test_apply_chain_entry_to_harnessed_explicit() -> None:
    out = _apply_chain_entry(_harnessed_cfg(), "harnessed:claude-agent-sdk:anthropic/claude-opus-4-6")
    assert out.executor_config.type == "harnessed"
    assert out.executor_config.model == "claude-agent-sdk"  # harness name → runner selection
    assert out.executor_config.harness == "claude-agent-sdk"
    assert out.llm_model == "anthropic/claude-opus-4-6"
    # AgentConfig.model returns the llm_model override for harnessed executors.
    assert out.model == "anthropic/claude-opus-4-6"


def test_apply_chain_entry_to_harnessed_default() -> None:
    out = _apply_chain_entry(_harnessed_cfg(), "harnessed:codex-cli")
    assert out.executor_config.type == "harnessed"
    assert out.executor_config.model == "codex-cli"
    assert out.llm_model is None
    # No explicit model → CLI uses its own default (AgentConfig.model is "").
    assert out.model == ""


def test_apply_chain_entry_preserves_other_executor_fields() -> None:
    base = _harnessed_cfg()
    base.executor_config.max_tool_result_chars = 12345
    out = _apply_chain_entry(base, "raw:deepseek/deepseek-chat")
    assert out.executor_config.max_tool_result_chars == 12345


def test_config_to_canonical() -> None:
    assert _config_to_canonical(_harnessed_cfg()) == "harnessed:claude-code-cli"
    raw = _apply_chain_entry(_harnessed_cfg(), "raw:deepseek/deepseek-chat")
    assert _config_to_canonical(raw) == "raw:deepseek/deepseek-chat"
    explicit = _apply_chain_entry(_harnessed_cfg(), "harnessed:claude-code-cli:anthropic/claude-opus-4-6")
    assert _config_to_canonical(explicit) == "harnessed:claude-code-cli:anthropic/claude-opus-4-6"


def test_collect_failure_text_scans_events() -> None:
    events = [
        {"type": "system", "subtype": "init"},
        {"error": "CLI exited with code 1", "stderr": "usage limit reached, resets in 2h"},
    ]
    text = _collect_failure_text(events, "[ERROR] CLI exited with code 1", "")
    assert "usage limit reached" in text
    assert "CLI exited with code 1" in text


def test_collect_failure_text_includes_final_text() -> None:
    text = _collect_failure_text([], "", "429 too many requests")
    assert "429 too many requests" in text
