"""Tests for executors/providers.py — model string parsing, resolution, and provider config."""

import pytest
from ypl.agent_harness_service.executors.providers import (
    PROVIDERS,
    get_provider_config,
    is_openai_compatible,
    parse_model_string,
    resolve_model,
)


class TestParseModelString:
    def test_valid_anthropic_model(self) -> None:
        provider, model_id = parse_model_string("anthropic/claude-sonnet-4-6")
        assert provider == "anthropic"
        assert model_id == "claude-sonnet-4-6"

    def test_valid_openai_model(self) -> None:
        provider, model_id = parse_model_string("openai/gpt-4o")
        assert provider == "openai"
        assert model_id == "gpt-4o"

    def test_valid_zai_model(self) -> None:
        provider, model_id = parse_model_string("zai/glm-5")
        assert provider == "zai"
        assert model_id == "glm-5"

    def test_valid_minimax_model(self) -> None:
        provider, model_id = parse_model_string("minimax/MiniMax-M2.5")
        assert provider == "minimax"
        assert model_id == "MiniMax-M2.5"

    def test_valid_moonshot_model(self) -> None:
        provider, model_id = parse_model_string("moonshot/kimi-k2.5")
        assert provider == "moonshot"
        assert model_id == "kimi-k2.5"

    def test_model_with_multiple_slashes(self) -> None:
        """Only the first slash is used as delimiter — model_id can contain slashes."""
        provider, model_id = parse_model_string("anthropic/some/nested/model")
        assert provider == "anthropic"
        assert model_id == "some/nested/model"

    def test_no_slash_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid model format"):
            parse_model_string("anthropic-claude-sonnet")

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider"):
            parse_model_string("gemini/gemini-pro")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid model format"):
            parse_model_string("")

    def test_slash_only_raises(self) -> None:
        """Slash-only string has empty provider, which is unknown."""
        with pytest.raises(ValueError, match="Unknown provider"):
            parse_model_string("/model-name")


class TestResolveModel:
    def test_param_model_takes_precedence(self) -> None:
        result = resolve_model(
            param_model="anthropic/claude-sonnet-4-6",
            agent_model="openai/gpt-4o",
            parent_model="zai/glm-5",
            default_model="minimax/MiniMax-M2.5",
        )
        assert result == "anthropic/claude-sonnet-4-6"

    def test_agent_model_used_when_no_param(self) -> None:
        result = resolve_model(
            param_model=None,
            agent_model="openai/gpt-4o",
            parent_model="zai/glm-5",
            default_model="minimax/MiniMax-M2.5",
        )
        assert result == "openai/gpt-4o"

    def test_parent_model_used_when_no_param_or_agent(self) -> None:
        result = resolve_model(
            param_model=None,
            agent_model=None,
            parent_model="zai/glm-5",
            default_model="minimax/MiniMax-M2.5",
        )
        assert result == "zai/glm-5"

    def test_default_model_used_as_fallback(self) -> None:
        result = resolve_model(
            param_model=None,
            agent_model=None,
            parent_model=None,
            default_model="minimax/MiniMax-M2.5",
        )
        assert result == "minimax/MiniMax-M2.5"

    def test_empty_strings_treated_as_missing(self) -> None:
        """Empty strings are falsy — the next source in chain is used."""
        result = resolve_model(
            param_model="",
            agent_model="",
            parent_model="anthropic/claude-haiku-4-5",
            default_model=None,
        )
        assert result == "anthropic/claude-haiku-4-5"

    def test_no_model_anywhere_raises(self) -> None:
        with pytest.raises(ValueError, match="No model specified"):
            resolve_model(None, None, None, None)

    def test_invalid_model_format_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid model format"):
            resolve_model(param_model="not-a-valid-format", agent_model=None, parent_model=None, default_model=None)

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider"):
            resolve_model(param_model="gemini/pro", agent_model=None, parent_model=None, default_model=None)


class TestGetProviderConfig:
    def test_anthropic_config(self) -> None:
        cfg = get_provider_config("anthropic")
        assert cfg.env_key == "ANTHROPIC_API_KEY"
        assert cfg.sdk == "anthropic"
        assert "anthropic.com" in cfg.api_base

    def test_openai_config(self) -> None:
        cfg = get_provider_config("openai")
        assert cfg.env_key == "OPENAI_API_KEY"
        assert cfg.sdk == "openai"

    def test_all_known_providers_exist(self) -> None:
        for provider in ("anthropic", "openai", "zai", "minimax", "moonshot"):
            cfg = get_provider_config(provider)
            assert cfg.env_key
            assert cfg.sdk in ("anthropic", "openai")

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider"):
            get_provider_config("google")

    def test_empty_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider"):
            get_provider_config("")


class TestIsOpenAICompatible:
    def test_openai_is_compatible(self) -> None:
        assert is_openai_compatible("openai") is True

    def test_anthropic_is_not_compatible(self) -> None:
        assert is_openai_compatible("anthropic") is False

    def test_zai_is_compatible(self) -> None:
        """ZAI uses the OpenAI-compatible SDK."""
        assert is_openai_compatible("zai") is True

    def test_minimax_is_compatible(self) -> None:
        assert is_openai_compatible("minimax") is True

    def test_moonshot_is_compatible(self) -> None:
        assert is_openai_compatible("moonshot") is True

    def test_all_non_anthropic_are_openai_compatible(self) -> None:
        """All providers except anthropic use the openai-compatible SDK."""
        for name in PROVIDERS:
            if name == "anthropic":
                assert not is_openai_compatible(name)
            else:
                assert is_openai_compatible(name), f"{name} should be OpenAI-compatible"
