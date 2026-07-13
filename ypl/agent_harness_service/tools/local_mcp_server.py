"""Re-export shim for local_mcp_server — backwards-compatibility wrapper.

The original ~2544-line monolith has been split into focused modules under
``ypl/agent_harness_service/tools/``.  This shim:

1. Imports all internal-only AHS tool submodules, triggering ``@mcp.tool()``
   registration on the harness FastMCP instance defined in ``mcp_instance``.
2. Imports the shared / external-data tool submodules from
   ``ypl/mcp_server/tools/`` so their ``@shared_tool(...)`` decorators
   register them on the harness MCP as well as platform MCP. Without this
   the harness mount would expose only internal tools — agents calling
   ``query_appdb`` / ``search_gcp_logs`` / etc. via ``/mcp/harness``
   would get a "no such tool" error and fall back to the (now-removed)
   AHS → platform detour through ``PLATFORM_MCP_TOKEN``.
3. Re-exports every public symbol that existing callers import from here,
   so no other file needs to change.

Existing imports that continue to work::

    from ypl.agent_harness_service.tools.local_mcp_server import mcp as harness_mcp
    from ypl.agent_harness_service.tools.local_mcp_server import register_orchestration_callbacks
    from ypl.agent_harness_service.tools.local_mcp_server import (
        clear_session_state, set_session_current_user,
        set_session_sandbox, clear_session_sandbox,
        reset_turn_websearch_count, clear_session_websearch_count,
    )
"""

# ---------------------------------------------------------------------------
# 1. Import the shared MCP instance and all public session-state helpers.
#    These must come first so that @mcp.tool() decorators in submodules can
#    find the ``mcp`` object.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 2. Import all internal-only AHS tool submodules to trigger @mcp.tool()
#    registration. Import order does not affect tool registration order
#    (FastMCP collects decorators lazily), but keeping it consistent helps
#    readability.
# ---------------------------------------------------------------------------
from ypl.agent_harness_service.tools import (  # noqa: F401
    agent_messaging,
    fork,
    gateway_tools,
    github_auth,
    linear,
    sandboxed_ops,
    scheduling,
    self_management,
    skills,
    subagents,
    workspace,
)
from ypl.agent_harness_service.tools.mcp_instance import (  # noqa: F401
    _validate_session_id,
    clear_session_sandbox,
    clear_session_state,
    clear_session_websearch_count,
    mcp,
    register_orchestration_callbacks,
    reset_turn_websearch_count,
    set_session_current_user,
    set_session_sandbox,
)

# ---------------------------------------------------------------------------
# 3. Import shared / external-data tool submodules from ypl.mcp_server.tools.
#    Each module's ``@shared_tool(...)`` decorator registers on every MCP
#    instance in ``ypl.mcp_common.shared_tool._INSTANCES`` — currently both
#    harness and platform. Without these imports, agents talking to the
#    harness mount only see internal AHS tools (bash, workspace, subagents,
#    etc.), not the AHS-system or external-data tools.
#
#    Mono-server callers also import ``ypl.mcp_server.mcp_tools`` from
#    ``ypl/mono_server/server.py``; Python's module cache makes that a
#    no-op so each tool registers exactly once on each instance.
# ---------------------------------------------------------------------------
from ypl.mcp_server.tools import (  # noqa: F401
    agent_artifacts,
    agent_schedules,
    database,
    gcp_logs,
    linear_sync,
    memory_artifacts,
    project_tasks,
    redis,
    security_incidents,
    sentry,
    slack,
    twitter,
)

# ---------------------------------------------------------------------------
# 3. Standalone entry point (unchanged from original).
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
