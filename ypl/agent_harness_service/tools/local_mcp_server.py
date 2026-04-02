"""Re-export shim for local_mcp_server — backwards-compatibility wrapper.

The original ~2544-line monolith has been split into focused modules under
``ypl/agent_harness_service/tools/``.  This shim:

1. Imports all tool submodules, triggering ``@mcp.tool()`` registration on the
   shared FastMCP instance defined in ``mcp_instance``.
2. Re-exports every public symbol that existing callers import from here,
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
# 2. Import all tool submodules to trigger @mcp.tool() registration.
#    Import order does not affect tool registration order (FastMCP collects
#    decorators lazily), but keeping it consistent helps readability.
# ---------------------------------------------------------------------------
from ypl.agent_harness_service.tools import (  # noqa: F401
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
# 3. Standalone entry point (unchanged from original).
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
