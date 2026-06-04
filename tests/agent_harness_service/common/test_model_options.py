"""Tests for model_options: enumeration, chain encoding, rate-limit detection,
and the Redis-backed global fallback chain."""

import pytest
from ypl.agent_harness_service.common.model_options import (
    DEFAULT_FALLBACK_CHAIN,
    ModelOption,
    canonical_to_display_label,
    chain_entry_to_executor,
    display_label_to_canonical,
    enumerate_model_options,
    format_chain_entry,
    get_fallback_chain,
    is_rate_limit_failure,
    parse_chain_entry,
    set_fallback_chain,
)


def test_enumerate_includes_harness_defaults_and_raw() -> None:
    options = enumerate_model_options()
    labels = {o.display_label for o in options}
    # Harness-default options exist for the known harnesses.
    assert "[HARNESSED] claude-code-cli (default)" in labels
    assert "[HARNESSED] codex-cli (default)" in labels
    # Raw option for every known model.
    assert "[RAW] deepseek/deepseek-v4-pro" in labels
    assert "[RAW] deepseek/deepseek-v4-flash" in labels
    assert "[RAW] anthropic/claude-opus-4-6" in labels


def test_enumerate_is_sorted_by_label() -> None:
    labels = [o.display_label for o in enumerate_model_options()]
    assert labels == sorted(labels)


def test_harnessed_explicit_only_for_mapped_providers() -> None:
    options = enumerate_model_options()
    harnessed_explicit = [o for o in options if o.executor_type == "harnessed" and o.provider is not None]
    providers = {o.provider for o in harnessed_explicit}
    # Only anthropic + openai have CLI harnesses.
    assert providers == {"anthropic", "openai"}
    # deepseek (no harness) must not appear as a harnessed explicit option.
    assert not any(o.provider == "deepseek" for o in harnessed_explicit)


@pytest.mark.parametrize(
    "entry",
    [
        "harnessed:claude-code-cli",
        "harnessed:claude-agent-sdk:anthropic/claude-opus-4-6",
        "harnessed:claude-code-cli:anthropic/claude-opus-4-6",
        "raw:deepseek/deepseek-v4-pro",
    ],
)
def test_roundtrip_parse_format(entry: str) -> None:
    opt = parse_chain_entry(entry)
    assert format_chain_entry(opt) == entry
    # display label round-trips back to the same canonical entry
    assert display_label_to_canonical(canonical_to_display_label(entry)) == entry


def test_chain_entry_to_executor_mapping() -> None:
    assert chain_entry_to_executor("raw:deepseek/deepseek-v4-pro") == ("raw", None, "deepseek/deepseek-v4-pro")
    assert chain_entry_to_executor("harnessed:claude-code-cli") == ("harnessed", "claude-code-cli", None)
    assert chain_entry_to_executor("harnessed:claude-agent-sdk:anthropic/claude-opus-4-6") == (
        "harnessed",
        "claude-agent-sdk",
        "anthropic/claude-opus-4-6",
    )
    assert chain_entry_to_executor("harnessed:codex-cli:openai/gpt-4o") == ("harnessed", "codex-cli", "openai/gpt-4o")


@pytest.mark.parametrize(
    "bad",
    [
        "noColon",
        "bogus:anthropic/claude-opus-4-6",  # unknown kind
        "raw:claude-code-cli",  # raw requires provider/model
        "harnessed:not-a-harness",  # unknown harness
        "raw:unknownprovider/foo",  # unknown provider
        "harnessed:anthropic/claude-opus-4-6",  # old form: harness must be named
        "harnessed:not-a-harness:anthropic/claude-opus-4-6",  # unknown harness w/ model
        "harnessed:claude-code-cli:unknownprovider/foo",  # unknown provider w/ harness
    ],
)
def test_parse_chain_entry_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_chain_entry(bad)


def test_display_label_to_canonical_unknown() -> None:
    with pytest.raises(ValueError):
        display_label_to_canonical("[RAW] not/a-real-model")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Error 429 Too Many Requests", True),
        ("usage limit reached, resets in 3h", True),
        ("Anthropic API overloaded", True),
        ("RATE LIMIT exceeded", True),
        (None, False),
        ("", False),
        ("file not found", False),
        ("CLI exited with code 1", False),
    ],
)
def test_is_rate_limit_failure(text: str | None, expected: bool) -> None:
    assert is_rate_limit_failure(text) is expected


def test_model_option_canonical_and_label() -> None:
    raw = ModelOption(executor_type="raw", provider="deepseek", model_id="deepseek-chat")
    assert raw.canonical == "raw:deepseek/deepseek-chat"
    assert raw.display_label == "[RAW] deepseek/deepseek-chat"
    harness_default = ModelOption(executor_type="harnessed", harness="claude-code-cli")
    assert harness_default.canonical == "harnessed:claude-code-cli"
    assert harness_default.llm_model is None


async def test_get_fallback_chain_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get(name: str, default=None):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr("ypl.backend.feature_flags.get_feature_value", _fake_get)
    chain = await get_fallback_chain()
    assert chain == DEFAULT_FALLBACK_CHAIN


async def test_get_fallback_chain_drops_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get(name: str, default=None):  # type: ignore[no-untyped-def]
        return ["raw:deepseek/deepseek-chat", "garbage-entry", "harnessed:codex-cli"]

    monkeypatch.setattr("ypl.backend.feature_flags.get_feature_value", _fake_get)
    chain = await get_fallback_chain()
    assert chain == ["raw:deepseek/deepseek-chat", "harnessed:codex-cli"]


async def test_get_fallback_chain_all_invalid_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get(name: str, default=None):  # type: ignore[no-untyped-def]
        return ["garbage", 123]

    monkeypatch.setattr("ypl.backend.feature_flags.get_feature_value", _fake_get)
    chain = await get_fallback_chain()
    assert chain == DEFAULT_FALLBACK_CHAIN


async def test_set_fallback_chain_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: dict[str, object] = {}

    async def _fake_set(name: str, value: object) -> None:
        saved[name] = value

    monkeypatch.setattr("ypl.backend.feature_flags.set_feature_value", _fake_set)
    await set_fallback_chain(["raw:deepseek/deepseek-chat"])
    assert saved["agent_fallback_chain"] == ["raw:deepseek/deepseek-chat"]

    with pytest.raises(ValueError):
        await set_fallback_chain(["totally-invalid"])
