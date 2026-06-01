"""Runtime configuration for the CLI.

Layered config (later layers override earlier ones):

1. Defaults (``DEFAULT_API_URL``).
2. ``~/.ahs/config.toml`` (if present) — TOML with top-level keys
   ``api_url``, ``api_key``, ``user_id``.
3. Environment variables ``AHS_API_URL``, ``AHS_API_KEY``,
   ``AHS_USER_ID``.

CLI flags (``--user-id``) override everything via the
:func:`resolve_config` ``override_*`` arguments.

No I/O happens at import time — everything is lazy so tests can stub
``HOME`` or pre-set env vars and call ``resolve_config()`` deterministically.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_API_URL = "https://ahs.agcouch.com"
DEFAULT_CONFIG_PATH = Path("~/.ahs/config.toml")


@dataclass(frozen=True)
class Config:
    """Resolved runtime config for a single CLI invocation."""

    api_url: str
    api_key: str | None
    user_id: str | None


class ConfigError(Exception):
    """Raised when required config is missing or malformed."""


def _load_config_file(path: Path) -> dict[str, str]:
    """Read ``path`` if it exists; return an empty dict otherwise.

    Tolerates an unreadable / malformed file by surfacing a ``ConfigError``
    so the operator can fix it instead of silently falling back to env-only.
    """
    expanded = path.expanduser()
    if not expanded.is_file():
        return {}
    try:
        with expanded.open("rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"Could not read {expanded}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Malformed TOML in {expanded}: {exc}") from exc
    # We only care about a flat top-level table; ignore unknown keys.
    out: dict[str, str] = {}
    for key in ("api_url", "api_key", "user_id"):
        value = data.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ConfigError(f"{expanded}: '{key}' must be a string, got {type(value).__name__}")
        out[key] = value
    return out


def resolve_config(
    *,
    override_user_id: str | None = None,
    config_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    """Resolve the final config from defaults + file + env + flag overrides.

    ``env`` defaults to ``os.environ`` and is overridable for tests.
    ``config_path`` defaults to ``~/.ahs/config.toml``.
    """
    env = env if env is not None else dict(os.environ)
    file_cfg = _load_config_file(config_path or DEFAULT_CONFIG_PATH)

    api_url = env.get("AHS_API_URL") or file_cfg.get("api_url") or DEFAULT_API_URL
    api_key = env.get("AHS_API_KEY") or file_cfg.get("api_key")
    user_id = override_user_id or env.get("AHS_USER_ID") or file_cfg.get("user_id")
    return Config(api_url=api_url.rstrip("/"), api_key=api_key, user_id=user_id)


def require_api_key(cfg: Config) -> str:
    """Return ``cfg.api_key`` or raise a friendly :class:`ConfigError`."""
    if not cfg.api_key:
        raise ConfigError(
            'AHS_API_KEY is not set. Export it in your shell or add `api_key = "..."` to ~/.ahs/config.toml.'
        )
    return cfg.api_key


def require_user_id(cfg: Config) -> str:
    """Return ``cfg.user_id`` or raise a friendly :class:`ConfigError`."""
    if not cfg.user_id:
        raise ConfigError(
            'user_id is not set. Pass --user-id, export AHS_USER_ID, or set `user_id = "..."` in ~/.ahs/config.toml.'
        )
    return cfg.user_id
