"""Layered config resolution: defaults / file / env / CLI override."""

from __future__ import annotations

from pathlib import Path

import pytest
from ahs_memory.config import DEFAULT_API_URL, ConfigError, require_api_key, require_user_id, resolve_config


def test_defaults_when_nothing_set(tmp_path: Path) -> None:
    cfg = resolve_config(env={}, config_path=tmp_path / "missing.toml")
    assert cfg.api_url == DEFAULT_API_URL
    assert cfg.api_key is None
    assert cfg.user_id is None


def test_env_overrides_defaults(tmp_path: Path) -> None:
    env = {"AHS_API_URL": "http://local:8090/", "AHS_API_KEY": "k", "AHS_USER_ID": "u"}
    cfg = resolve_config(env=env, config_path=tmp_path / "missing.toml")
    # Trailing slash stripped for consistency with how routes concatenate.
    assert cfg.api_url == "http://local:8090"
    assert cfg.api_key == "k"
    assert cfg.user_id == "u"


def test_file_provides_fallback(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('api_url = "http://file"\napi_key = "filekey"\nuser_id = "fileuser"\n', encoding="utf-8")
    cfg = resolve_config(env={}, config_path=cfg_file)
    assert cfg.api_url == "http://file"
    assert cfg.api_key == "filekey"
    assert cfg.user_id == "fileuser"


def test_env_wins_over_file(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('api_url = "http://file"\napi_key = "filekey"\n', encoding="utf-8")
    cfg = resolve_config(env={"AHS_API_URL": "http://env"}, config_path=cfg_file)
    assert cfg.api_url == "http://env"
    assert cfg.api_key == "filekey"


def test_cli_override_user_id_wins(tmp_path: Path) -> None:
    cfg = resolve_config(
        env={"AHS_USER_ID": "env-user"},
        config_path=tmp_path / "missing.toml",
        override_user_id="cli-user",
    )
    assert cfg.user_id == "cli-user"


def test_malformed_toml_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("not toml = = =", encoding="utf-8")
    with pytest.raises(ConfigError):
        resolve_config(env={}, config_path=cfg_file)


def test_non_string_value_in_toml_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("api_url = 42\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        resolve_config(env={}, config_path=cfg_file)


def test_require_api_key_raises_when_missing(tmp_path: Path) -> None:
    cfg = resolve_config(env={}, config_path=tmp_path / "missing.toml")
    with pytest.raises(ConfigError):
        require_api_key(cfg)


def test_require_user_id_raises_when_missing(tmp_path: Path) -> None:
    cfg = resolve_config(env={"AHS_API_KEY": "k"}, config_path=tmp_path / "missing.toml")
    with pytest.raises(ConfigError):
        require_user_id(cfg)


def test_require_helpers_return_value_when_set(tmp_path: Path) -> None:
    cfg = resolve_config(env={"AHS_API_KEY": "k", "AHS_USER_ID": "u"}, config_path=tmp_path / "missing.toml")
    assert require_api_key(cfg) == "k"
    assert require_user_id(cfg) == "u"
