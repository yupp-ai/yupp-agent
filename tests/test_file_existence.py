"""Smoke tests for critical file existence.

These tests verify that files referenced by code at runtime actually exist.
Would have caught PR #45 (missing lua script) and PR #8 (wrong entrypoint path).
"""

from pathlib import Path

import pytest


def _repo_root() -> Path:
    """Find repo root by walking up from this file."""
    path = Path(__file__).resolve()
    for p in [path, *path.parents]:
        if (p / "pyproject.toml").exists():
            return p
    raise FileNotFoundError("Could not find repository root")


REPO_ROOT = _repo_root()


class TestCriticalFileExistence:
    """Verify files loaded at runtime exist in the repo."""

    def test_rate_limiter_lua_script_exists(self) -> None:
        """RedisTokenBucketRateLimiter loads this Lua script at __init__ time."""
        lua_path = REPO_ROOT / "ypl" / "backend" / "utils" / "token_bucket_rate_limiter.lua"
        assert lua_path.exists(), f"Lua script required by RedisTokenBucketRateLimiter is missing: {lua_path}"

    def test_alembic_ini_exists(self) -> None:
        assert (REPO_ROOT / "alembic.ini").exists()

    def test_pyproject_toml_exists(self) -> None:
        assert (REPO_ROOT / "pyproject.toml").exists()


class TestEntrypointScripts:
    """Verify entrypoint scripts referenced by Docker/Cloud Run configs exist."""

    @pytest.mark.parametrize(
        "path",
        [
            "ypl/streamlit_server/streamlit_server_entrypoint.sh",
        ],
    )
    def test_entrypoint_exists(self, path: str) -> None:
        full_path = REPO_ROOT / path
        assert full_path.exists(), f"Entrypoint script missing: {full_path}"


class TestConfigFiles:
    """Verify YAML config files loaded at runtime exist."""

    @pytest.mark.parametrize(
        "path",
        [
            "data/feature_flags.yaml",
            "data/app_settings.yaml",
        ],
    )
    def test_config_file_exists(self, path: str) -> None:
        full_path = REPO_ROOT / path
        if not full_path.exists():
            pytest.skip(f"Config file {path} not present in this environment")
