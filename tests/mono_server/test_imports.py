"""Smoke tests verifying all mono_server modules are importable.

These tests catch import errors — missing deps, circular imports, or broken
module references — early in CI before any integration tests run.
"""

from __future__ import annotations
import importlib

MONO_SERVER_MODULES = [
    "ypl.mono_server",
    "ypl.mono_server.config",
    "ypl.mono_server.server",
]


def test_mono_server_imports() -> None:
    """All mono_server modules resolve without import errors."""
    for mod_path in MONO_SERVER_MODULES:
        mod = importlib.import_module(mod_path)
        assert mod is not None, f"Failed to import {mod_path}"


def test_config_importable() -> None:
    """MonoConfig is importable and instantiable with defaults."""
    from ypl.mono_server.config import MonoConfig

    cfg = MonoConfig()
    assert cfg.port == 8090
    assert cfg.gateway_slack_enabled is True
    assert cfg.gateway_github_enabled is True


def test_server_exports_app() -> None:
    """The server module exposes a FastAPI ``app`` instance."""
    from fastapi import FastAPI
    from ypl.mono_server.server import app

    assert isinstance(app, FastAPI)


def test_server_exports_create_app() -> None:
    """The server module exposes the ``create_app`` factory."""
    from ypl.mono_server.server import create_app

    assert callable(create_app)


def test_lifespan_importable() -> None:
    """The combined_lifespan context manager is importable."""
    from ypl.mono_server.server import combined_lifespan

    assert callable(combined_lifespan)


def test_sub_app_instances_importable() -> None:
    """Module-level MCP sub-apps are importable."""
    from ypl.mono_server.server import harness_mcp_app, yuppster_mcp_http_app

    assert harness_mcp_app is not None
    assert yuppster_mcp_http_app is not None
