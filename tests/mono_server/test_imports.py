"""Smoke tests verifying all mono_server modules are importable.

These tests catch import errors — missing deps, circular imports, or broken
module references — early in CI before any integration tests run.
"""

from __future__ import annotations
import importlib

MONO_SERVER_MODULES = [
    "ypl.mono_server",
    "ypl.mono_server.config",
    "ypl.mono_server.gateway_plugin",
    "ypl.mono_server.plugins",
    "ypl.mono_server.plugins.slack",
    "ypl.mono_server.server",
    "ypl.mono_server.unified_mcp",
]


def test_mono_server_imports() -> None:
    """All mono_server modules resolve without import errors."""
    for mod_path in MONO_SERVER_MODULES:
        mod = importlib.import_module(mod_path)
        assert mod is not None, f"Failed to import {mod_path}"


def test_config_importable() -> None:
    """MonoConfig is importable, instantiable, and exposes the expected fields.

    The two master flags ``ahs_mono_enable_gateway_service`` and
    ``ahs_mono_enable_mcp`` default to ``False`` in production. The mono-server
    conftest sets them to ``true`` for the legacy "everything on" baseline,
    so we just assert presence and type here — see
    ``test_master_flags.TestMonoConfigMasterFlagDefaults`` for the actual
    default-value tests.
    """
    from ypl.mono_server.config import MonoConfig

    cfg = MonoConfig()
    assert cfg.port == 8090
    assert isinstance(cfg.ahs_mono_enable_gateway_service, bool)
    assert isinstance(cfg.ahs_mono_enable_mcp, bool)
    assert cfg.gateway_slack_enabled is True
    assert cfg.gateway_github_enabled is False  # off by default — requires explicit opt-in


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
    """Both MCP sub-apps are importable from server and unified_mcp."""
    from ypl.mono_server.server import agcouch_mcp_http_app as server_agcouch
    from ypl.mono_server.server import harness_mcp_http_app as server_harness
    from ypl.mono_server.unified_mcp import agcouch_mcp_http_app, harness_mcp_http_app

    assert harness_mcp_http_app is not None
    assert agcouch_mcp_http_app is not None
    # server.py re-exports the same objects (imported from unified_mcp).
    assert server_harness is harness_mcp_http_app
    assert server_agcouch is agcouch_mcp_http_app
    # They are distinct apps — no shared tool registry.
    assert harness_mcp_http_app is not agcouch_mcp_http_app


def test_unified_mcp_exports() -> None:
    """unified_mcp module exports the expected public symbols."""
    from ypl.mono_server.unified_mcp import (
        AgcouchMcpAuthMiddleware,
        HarnessMcpAuthMiddleware,
        agcouch_mcp_http_app,
        harness_mcp_http_app,
    )

    assert harness_mcp_http_app is not None
    assert agcouch_mcp_http_app is not None
    assert HarnessMcpAuthMiddleware is not None
    assert AgcouchMcpAuthMiddleware is not None
