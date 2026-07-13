"""Regression tests: every platform tool module must be imported so its
``@mcp_server.tool()`` decorators actually fire at process start.

The side-effect imports in ``ypl/mcp_server/mcp_tools.py`` are pure runtime
imports — ruff can't see that they have side effects, so without these
tests an auto-fix (or a refactor) can silently delete them and the platform
MCP server ends up serving zero tools. That's how phase 8 shipped (PR
#215): agent sessions got a working ``.mcp.json`` pointing at platform but
``tools/list`` came back empty, and the agent said "I don't have a
creation tool loaded."

These tests fail loudly instead.
"""

from __future__ import annotations

import pytest


def test_platform_tools_register_on_import() -> None:
    """Importing mcp_tools must populate the platform FastMCP instance.

    If this fails (count == 0), check that ``ypl/mcp_server/mcp_tools.py``
    still has the ``import ypl.mcp_server.tools.*`` side-effect imports.
    """
    # Import the orchestrator — this fires every tool module's
    # @mcp_server.tool() decorators.
    import ypl.mcp_server.mcp_tools  # noqa: F401
    from ypl.mcp_server.core import mcp_server

    # get_tools() is async on FastMCP; avoid an event loop by poking the
    # internal registry directly. The _tool_manager attribute is stable
    # across the fastmcp versions we pin.
    manager = getattr(mcp_server, "_tool_manager", None)
    assert manager is not None, "mcp_server has no _tool_manager — fastmcp internals changed?"
    tools = getattr(manager, "_tools", None)
    assert tools is not None, "mcp_server._tool_manager has no _tools dict"
    assert len(tools) > 0, (
        "platform FastMCP has zero tools — the side-effect imports in "
        "ypl/mcp_server/mcp_tools.py were likely removed. "
        "Restore `import ypl.mcp_server.tools.*` for each module."
    )


def test_critical_platform_tools_are_registered() -> None:
    """Name-level check on the tools we rely on most day-to-day.

    If any of these go missing, agents can't do their core work — e.g.
    add_artifact, query_appdb, save_memory.
    """
    import ypl.mcp_server.mcp_tools  # noqa: F401
    from ypl.mcp_server.core import mcp_server

    manager = getattr(mcp_server, "_tool_manager", None)
    assert manager is not None
    tools_dict = getattr(manager, "_tools", None)
    assert tools_dict is not None
    tool_names = set(tools_dict.keys())

    required = {
        # Artifact lifecycle (agent_artifacts.py) — unified surface, post-PR #230
        "add_artifact",
        "update_artifact",
        "update_artifact_content",
        "read_artifact",
        "list_artifacts",
        "list_artifact_versions",
        "search_artifacts",
        "archive_artifact",
        "archive_artifact_slug",
        "artifact_url",
        # Databases (database.py)
        "query_appdb",
        "query_agentdb",
        # Agent memory (memory_artifacts.py) — DB-backed MEMORY artifacts
        "save_memory",
        "load_memory",
        "search_memory",
        "list_memory",
    }
    missing = required - tool_names
    assert not missing, (
        f"Missing platform tools: {sorted(missing)}.\n"
        f"Registered: {sorted(tool_names)[:15]}…\n"
        f"This almost always means the corresponding ``import ypl.mcp_server.tools.*`` "
        f"side-effect import is missing from ``ypl/mcp_server/mcp_tools.py``."
    )


def test_legacy_gcs_memory_tools_are_not_registered() -> None:
    """Regression guard: the GCS-backed memory tools are gone for good.

    Phase [6] of the "unify memory into artifacts" project deleted
    ``ypl/mcp_server/tools/agent_memory.py`` and removed its side-effect
    import from ``mcp_tools.py``. The new memory surface is
    ``save_memory`` / ``load_memory`` / ``search_memory`` / ``list_memory``
    in ``ypl/mcp_server/tools/memory_artifacts.py``.

    If anyone re-introduces the legacy module (or re-registers any of
    the legacy tool names from somewhere else), this test fails so the
    revert is loud.
    """
    import importlib

    import ypl.mcp_server.mcp_tools  # noqa: F401
    from ypl.mcp_server.core import mcp_server

    # The legacy module must not exist as an importable submodule.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ypl.mcp_server.tools.agent_memory")

    # And the legacy tool names must not be registered on the server.
    manager = getattr(mcp_server, "_tool_manager", None)
    assert manager is not None
    tools_dict = getattr(manager, "_tools", None)
    assert tools_dict is not None
    tool_names = set(tools_dict.keys())

    legacy_names = {"get_agent_memory", "store_agent_memory", "search_agent_memory"}
    leaked = legacy_names & tool_names
    assert not leaked, (
        f"Legacy GCS-backed memory tools must not be re-registered: {sorted(leaked)}.\n"
        f"Use save_memory / load_memory / search_memory / list_memory instead."
    )
