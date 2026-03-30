"""Smoke tests verifying all AHS modules are importable from their new paths.

Each test function covers one component group and uses importlib.import_module()
so that failures produce clear, actionable error messages with the full module path.
"""

from __future__ import annotations
import importlib

# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------

COMMON_MODULES = [
    "ypl.agent_harness_service.common",
    "ypl.agent_harness_service.common.constants",
    "ypl.agent_harness_service.common.config",
    "ypl.agent_harness_service.common.models",
    "ypl.agent_harness_service.common.types",
    "ypl.agent_harness_service.common.exceptions",
    "ypl.agent_harness_service.common.agent_registry",
    "ypl.agent_harness_service.common.auth",
]


def test_common_imports() -> None:
    """All common modules resolve without import errors."""
    for mod_path in COMMON_MODULES:
        mod = importlib.import_module(mod_path)
        assert mod is not None, f"Failed to import {mod_path}"


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

CORE_MODULES = [
    "ypl.agent_harness_service.core",
    "ypl.agent_harness_service.core.streaming",
    "ypl.agent_harness_service.core.gcs_sync",
    "ypl.agent_harness_service.core.memory_persistence",
    "ypl.agent_harness_service.core.session_persistence",
    "ypl.agent_harness_service.core.session_title",
]


def test_core_imports() -> None:
    """All core modules resolve without import errors."""
    for mod_path in CORE_MODULES:
        mod = importlib.import_module(mod_path)
        assert mod is not None, f"Failed to import {mod_path}"


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

GATEWAY_MODULES = [
    "ypl.agent_harness_service.gateway",
    "ypl.agent_harness_service.gateway.base",
    "ypl.agent_harness_service.gateway.registry",
    "ypl.agent_harness_service.gateway.slack",
    "ypl.agent_harness_service.gateway.slack_prefetch",
]


def test_gateway_imports() -> None:
    """All gateway modules resolve without import errors."""
    for mod_path in GATEWAY_MODULES:
        mod = importlib.import_module(mod_path)
        assert mod is not None, f"Failed to import {mod_path}"


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------

EXECUTOR_MODULES = [
    "ypl.agent_harness_service.executors",
    "ypl.agent_harness_service.executors.runner",
    "ypl.agent_harness_service.executors.raw_executor",
    "ypl.agent_harness_service.executors.codex_runner",
    "ypl.agent_harness_service.executors.context",
    "ypl.agent_harness_service.executors.system_prompt",
    "ypl.agent_harness_service.executors.sandbox",
    "ypl.agent_harness_service.executors.providers",
]


def test_executor_imports() -> None:
    """All executor modules resolve without import errors."""
    for mod_path in EXECUTOR_MODULES:
        mod = importlib.import_module(mod_path)
        assert mod is not None, f"Failed to import {mod_path}"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

TOOLS_MODULES = [
    "ypl.agent_harness_service.tools",
    "ypl.agent_harness_service.tools.local_mcp_server",
    "ypl.agent_harness_service.tools.workspace_tools",
    "ypl.agent_harness_service.tools.repo_manager",
    "ypl.agent_harness_service.tools.mcp_client",
    "ypl.agent_harness_service.tools.github_token_storage",
]


def test_tools_imports() -> None:
    """All tools modules resolve without import errors."""
    for mod_path in TOOLS_MODULES:
        mod = importlib.import_module(mod_path)
        assert mod is not None, f"Failed to import {mod_path}"


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

PROJECTS_MODULES = [
    "ypl.agent_harness_service.projects",
    "ypl.agent_harness_service.projects.project_service",
    "ypl.agent_harness_service.projects.project_routes",
    "ypl.agent_harness_service.projects.project_types",
    "ypl.agent_harness_service.projects.task_utils",
    "ypl.agent_harness_service.projects.schedule_service",
]


def test_projects_imports() -> None:
    """All projects modules resolve without import errors."""
    for mod_path in PROJECTS_MODULES:
        mod = importlib.import_module(mod_path)
        assert mod is not None, f"Failed to import {mod_path}"


# ---------------------------------------------------------------------------
# Root (ypl.agent_harness_service)
# ---------------------------------------------------------------------------

ROOT_MODULES = [
    "ypl.agent_harness_service",
    "ypl.agent_harness_service.service",
    "ypl.agent_harness_service.server",
    "ypl.agent_harness_service.routes",
    "ypl.agent_harness_service.orchestration",
    "ypl.agent_harness_service.task_executor",
    "ypl.agent_harness_service.scheduler",
]


def test_root_imports() -> None:
    """All root-level modules resolve without import errors."""
    for mod_path in ROOT_MODULES:
        mod = importlib.import_module(mod_path)
        assert mod is not None, f"Failed to import {mod_path}"
