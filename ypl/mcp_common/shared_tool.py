"""Dual-registration decorator for tools that live on more than one MCP mount.

Background
----------
The yupp-agent platform runs two MCP HTTP mounts in mono mode:

* ``/mcp/harness`` — the AHS-internal mount that agent subprocesses talk to.
  Authenticated by ``AHS_MCP_SECRET`` (a shared secret between the AHS runner
  and the FastMCP app); routes through ``HarnessMcpAuthMiddleware``.
* ``/mcp/agcouch`` — the developer/IDE-facing mount. Authenticated by Google
  OAuth (or, until phase-5 retires them, ``yupp_dev_*`` Bearer tokens);
  routes through ``AgcouchMcpAuthMiddleware``.

Path-based mount is the *only* security boundary — a request that arrives on
``/mcp/harness`` cannot reach a tool registered solely on the agcouch
``FastMCP`` instance, and vice versa. Each instance has its own private tool
registry. The auth middleware never copies one registry into the other.

Tool taxonomy
-------------
Tools fall into one of three buckets:

1. **Internal-only** — bash, sandboxed_ops, workspace, skills, agent_messaging,
   subagents, github_auth, gateway_tools. Live in
   ``ypl/agent_harness_service/tools/`` and register on ``harness_mcp``
   directly via ``@mcp.tool(...)``. Never exposed on agcouch.
2. **Shared (AHS system)** — project_tasks, agent_artifacts, agent_schedules,
   memory_artifacts. Tools that engineers in their IDE *and* agents in a
   subprocess both need (managing the same projects, artifacts, schedules,
   memory). Register on both mounts via :func:`shared_tool`.
3. **External-data** — database, sentry, gcp_logs, redis, slack, twitter,
   linear_sync, security_incidents. Reach external systems (yuppdb, Sentry,
   GCP logs, etc.). Same dual-registration via :func:`shared_tool`, but the
   harness-side registration may be skipped at startup if required
   credentials aren't configured in this deployment — the corresponding
   tool simply doesn't appear in the agent's tool list. Agcouch always
   tries to register; calls to a credential-less tool fail at call time.

Why dual-registration is convenience, not auth
----------------------------------------------
Path is still the boundary. Dual-registration just removes the
"which-instance-do-I-write-this-on" question for tools that genuinely
belong on both. The decorator looks like this::

    @shared_tool(name="get_project", description="Look up a project by ID.")
    async def get_project(...): ...

…and registers ``get_project`` on every MCP instance in ``_INSTANCES``.
Adding a future external mount (e.g. a Runlayer-proxy MCP) is one line:
append the new ``FastMCP`` instance to that list — every shared/external
tool picks it up automatically.

Why this file lives in ``ypl/mcp_common/``
------------------------------------------
``shared_tool`` imports both ``ypl.agent_harness_service.tools.mcp_instance``
and ``ypl.mcp_server.core``. That import shape is identical to
``ypl/mono_server/server.py`` — a wiring file that composes AHS and the
agcouch MCP. We treat ``ypl.mcp_common.shared_tool`` as a wiring module:
documented as the one and only Layer-0 module that may import from both
``agent_harness_service`` and ``mcp_server``. See
``ypl/agent_harness_service/ARCHITECTURE.md`` for the architectural carve-out.
"""

from __future__ import annotations
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP

from ypl.agent_harness_service.tools.mcp_instance import mcp as harness_mcp
from ypl.backend.config import settings
from ypl.mcp_server.core import mcp_server as agcouch_mcp
from ypl.structured_logger import get_logger

logger = get_logger()


# Module-level list of MCP instances ``shared_tool`` writes to.
#
# Order matters for one observable behaviour: when a tool registers on
# multiple instances and a name collision happens, the last writer wins on
# its own instance — but the registries are independent, so this only
# matters within a single instance. We list ``harness_mcp`` first so its
# audit log entries (when a future request happens to hit it before the
# agcouch instance is loaded) come before agcouch's. There is no other
# cross-instance ordering effect.
_INSTANCES: list[FastMCP] = [harness_mcp, agcouch_mcp]


# ``FastMCP`` instances whose registration is gated on credentials being
# present in this process. When any setting in ``requires_settings`` is
# unset/empty we skip these instances — they are the deployments where the
# agent's tool list should not advertise something that will fail at call
# time. The agcouch mount is always tried so engineer IDE traffic still
# fails loudly with "yuppdb is not configured" rather than disappearing
# from listings.
_GATED_INSTANCES: frozenset[FastMCP] = frozenset({harness_mcp})


def _missing_settings(names: tuple[str, ...]) -> list[str]:
    """Return the subset of *names* whose ``settings`` attribute is unset.

    "Unset" means missing, ``None``, or empty string. Any truthy value is
    accepted — this is a register-time *probe*, not full validation. The
    actual tool call may still fail (e.g. wrong format, unreachable host)
    even when the setting is technically populated; that path is the
    agcouch tool's responsibility.
    """
    missing: list[str] = []
    for name in names:
        value = getattr(settings, name, None)
        if value in (None, ""):
            missing.append(name)
    return missing


def shared_tool(
    *args: Any,
    requires_settings: tuple[str, ...] = (),
    **kwargs: Any,
) -> Callable[[Callable[..., Any]], Any]:
    """Register a tool on every MCP instance in :data:`_INSTANCES`.

    Drop-in replacement for ``@mcp_server.tool(...)`` /
    ``@mcp.tool(...)``. Forwards every positional and keyword argument to
    each ``FastMCP.tool(...)`` call — ``name``, ``description``,
    ``annotations``, etc. all behave identically.

    Args:
        *args: Forwarded to ``FastMCP.tool``.
        requires_settings: Optional tuple of ``settings`` attribute names
            that must be truthy in this process for the tool to register on
            credential-gated instances (currently just the harness mount).
            Missing settings cause:

            - One ``logger.warning`` at startup naming the tool and the
              missing settings, so operators can see why the tool didn't
              show up in an agent's tool list.
            - Skipped registration on every instance in
              :data:`_GATED_INSTANCES`. The non-gated instances (agcouch)
              still register, so engineer-IDE traffic to the same tool
              fails loudly at call time rather than 404-ing the listing.

            When ``requires_settings`` is empty the gate is a no-op and
            every instance registers unconditionally.
        **kwargs: Forwarded to ``FastMCP.tool``.

    Returns:
        A decorator that registers ``fn`` on each instance and returns
        the resulting ``FunctionTool``. ``FunctionTool.fn`` points back
        at the original callable, so existing test code that pulls the
        raw coroutine via ``module.tool_name.fn`` continues to work
        unchanged. When registration is fully skipped (every instance
        gated and missing creds) we return the original ``fn`` so
        attribute access doesn't crash — that path also has nothing to
        register against, so the difference is invisible to test code.
    """
    missing = _missing_settings(requires_settings)

    def decorator(fn: Callable[..., Any]) -> Any:
        tool_name = kwargs.get("name") or fn.__name__
        # Track the first ``FunctionTool`` we register; we return it so
        # ``module.tool_name.fn`` still works at call sites that bypass
        # the runtime (notably the test suite).
        first_tool: Any = None

        if missing:
            skipped: list[str] = []
            for instance in _INSTANCES:
                if instance in _GATED_INSTANCES:
                    skipped.append(instance.name)
                    continue
                tool_obj = instance.tool(*args, **kwargs)(fn)
                if first_tool is None:
                    first_tool = tool_obj
            logger.warning(
                "Skipping shared tool registration on credential-gated MCP mounts",
                tool=tool_name,
                missing_settings=missing,
                skipped_mounts=skipped,
            )
            return first_tool if first_tool is not None else fn

        for instance in _INSTANCES:
            tool_obj = instance.tool(*args, **kwargs)(fn)
            if first_tool is None:
                first_tool = tool_obj
        return first_tool if first_tool is not None else fn

    return decorator
