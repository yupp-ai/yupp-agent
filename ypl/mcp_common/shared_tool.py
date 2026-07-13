"""Dual-registration decorator for tools that live on more than one MCP mount.

Background
----------
The yupp-agent platform runs two MCP HTTP mounts in mono mode:

* ``/mcp/harness`` — the AHS-internal mount that agent subprocesses talk to.
  Authenticated by ``AHS_MCP_SECRET`` (a shared secret between the AHS runner
  and the FastMCP app); routes through ``HarnessMcpAuthMiddleware``.
* ``/mcp/platform`` — the developer/IDE-facing mount. Authenticated by Google
  OAuth (or, until phase-5 retires them, ``yupp_dev_*`` Bearer tokens);
  routes through ``PlatformMcpAuthMiddleware``.

Path-based mount is the *only* security boundary — a request that arrives on
``/mcp/harness`` cannot reach a tool registered solely on the platform
``FastMCP`` instance, and vice versa. Each instance has its own private tool
registry. The auth middleware never copies one registry into the other.

Tool taxonomy
-------------
Tools fall into one of three buckets:

1. **Internal-only** — bash, sandboxed_ops, workspace, skills, agent_messaging,
   subagents, github_auth, gateway_tools. Live in
   ``ypl/agent_harness_service/tools/`` and register on ``harness_mcp``
   directly via ``@mcp.tool(...)``. Never exposed on platform.
2. **Shared (AHS system)** — project_tasks, agent_artifacts, agent_schedules,
   memory_artifacts. Tools that engineers in their IDE *and* agents in a
   subprocess both need (managing the same projects, artifacts, schedules,
   memory). Register on both mounts via :func:`shared_tool`.
3. **External-data** — database, sentry, gcp_logs, redis, slack, twitter,
   linear_sync, security_incidents. Reach external systems (appdb, Sentry,
   GCP logs, etc.). Same dual-registration via :func:`shared_tool`, but the
   harness-side registration may be skipped at startup if required
   credentials aren't configured in this deployment — the corresponding
   tool simply doesn't appear in the agent's tool list. Platform always
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
platform MCP. We treat ``ypl.mcp_common.shared_tool`` as a wiring module:
documented as the one and only Layer-0 module that may import from both
``agent_harness_service`` and ``mcp_server``. See
``ypl/agent_harness_service/ARCHITECTURE.md`` for the architectural carve-out.
"""

from __future__ import annotations
import os
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP

from ypl.agent_harness_service.tools.mcp_instance import mcp as harness_mcp
from ypl.backend.config import settings
from ypl.mcp_server.core import mcp_server as platform_mcp
from ypl.structured_logger import get_logger

logger = get_logger()


# Module-level list of MCP instances ``shared_tool`` writes to.
#
# Order matters for one observable behaviour: when a tool registers on
# multiple instances and a name collision happens, the last writer wins on
# its own instance — but the registries are independent, so this only
# matters within a single instance. We list ``harness_mcp`` first so its
# audit log entries (when a future request happens to hit it before the
# platform instance is loaded) come before platform's. There is no other
# cross-instance ordering effect.
_INSTANCES: list[FastMCP] = [harness_mcp, platform_mcp]


# ``FastMCP`` instances whose registration is gated on credentials being
# present in this process. When any setting in ``requires_settings`` is
# unset/empty we skip these instances — they are the deployments where the
# agent's tool list should not advertise something that will fail at call
# time. The platform mount is always tried so engineer IDE traffic still
# fails loudly with "appdb is not configured" rather than disappearing
# from listings.
_GATED_INSTANCES: frozenset[FastMCP] = frozenset({harness_mcp})


def _missing_settings(names: tuple[str, ...]) -> list[str]:
    """Return the subset of *names* that are unset on ``settings`` or in env.

    For each name, the probe order is:

    1. Pydantic-settings attribute on :data:`settings`.
    2. Raw process environment variable of the same name.

    Either being truthy clears the gate. This lets callers gate on credentials
    that don't live on ``Settings`` (e.g. ``SLACK_MCP_SERVER_APP_USER_TOKEN``,
    which the OpsBot client reads directly via ``os.environ.get``) without
    forcing every external token to first land on the pydantic class.

    "Unset" means missing on both sources or present-but-empty. Any
    truthy value clears the gate — this is a register-time *probe*, not
    full validation. The actual tool call may still fail (wrong format,
    unreachable host, expired credential) even when the setting is
    technically populated; that path is the tool's responsibility.
    """
    missing: list[str] = []
    for name in names:
        settings_val = getattr(settings, name, None)
        if settings_val:
            continue
        env_val = os.environ.get(name)
        if env_val:
            continue
        missing.append(name)
    return missing


_ADC_AVAILABLE_CACHE: bool | None = None


def _gcp_adc_available() -> bool:
    """Return ``True`` when Google Application Default Credentials resolve.

    Probes once per process and caches the result — ``google.auth.default()``
    is ~1 second on first call (it walks env, gcloud config, metadata server).

    A ``False`` here covers the reviewer's case-3 deployment shape: a host
    that has ``GCP_PROJECT_ID`` set (e.g. copied from prod) but no usable
    ADC chain. Without this probe the BigQuery / GCP-logs tools register
    on harness and fail at call time with an unhelpful auth error.
    """
    global _ADC_AVAILABLE_CACHE
    if _ADC_AVAILABLE_CACHE is not None:
        return _ADC_AVAILABLE_CACHE
    try:
        # Lazy import: google-auth is heavy and not all deployments need it.
        import google.auth

        google.auth.default()  # type: ignore[no-untyped-call]
        _ADC_AVAILABLE_CACHE = True
    except Exception:
        _ADC_AVAILABLE_CACHE = False
    return _ADC_AVAILABLE_CACHE


def shared_tool(
    *args: Any,
    requires_settings: tuple[str, ...] = (),
    requires_gcp_adc: bool = False,
    **kwargs: Any,
) -> Callable[[Callable[..., Any]], Any]:
    """Register a tool on every MCP instance in :data:`_INSTANCES`.

    Drop-in replacement for ``@mcp_server.tool(...)`` /
    ``@mcp.tool(...)``. Forwards every positional and keyword argument to
    each ``FastMCP.tool(...)`` call — ``name``, ``description``,
    ``annotations``, etc. all behave identically.

    Args:
        *args: Forwarded to ``FastMCP.tool``.
        requires_settings: Optional tuple of credential names that must be
            truthy in this process for the tool to register on
            credential-gated instances (currently just the harness mount).
            For each name we probe (in order) ``getattr(settings, name)``
            and ``os.environ.get(name)`` — either being truthy clears the
            gate. Missing creds cause:

            - One ``logger.warning`` at startup naming the tool and the
              missing settings, so operators can see why the tool didn't
              show up in an agent's tool list.
            - Skipped registration on every instance in
              :data:`_GATED_INSTANCES`. The non-gated instances (platform)
              still register, so engineer-IDE traffic to the same tool
              fails loudly at call time rather than 404-ing the listing.

            When ``requires_settings`` is empty *and* ``requires_gcp_adc``
            is ``False`` the gate is a no-op and every instance registers
            unconditionally.
        requires_gcp_adc: When ``True``, additionally probe Google
            Application Default Credentials (``google.auth.default()``)
            once at first decoration and treat a probe failure as if a
            named credential were missing. Use for tools that hit
            BigQuery / Cloud Logging / etc. — a deployment with
            ``GCP_PROJECT_ID`` set but no usable ADC chain (a fairly
            common case when copying envs from prod to dev) would
            otherwise register the tool and have it fail at call time.
        **kwargs: Forwarded to ``FastMCP.tool``.

    Returns:
        A decorator that registers ``fn`` on each instance and returns
        the resulting ``FunctionTool``. ``FunctionTool.fn`` points back
        at the original callable, so existing test code that pulls the
        raw coroutine via ``module.tool_name.fn`` continues to work
        unchanged.

    Raises:
        RuntimeError: at decoration time when *every* instance in
            :data:`_INSTANCES` is gated (i.e. covered by
            :data:`_GATED_INSTANCES`) **and** any required setting is
            missing. This is a misconfiguration — the tool would land
            on no MCP server but the decorator would still return a
            naked ``fn`` whose ``.fn`` attribute access (the documented
            pattern in this module's docstring) would crash with
            ``AttributeError`` later. Crashing loudly at startup is
            strictly better than the silent listing-disappears tripwire,
            and it forces phase-3 / phase-4 (which plan to drop platform
            from ``_INSTANCES``) to also revisit every gate.
    """
    missing = _missing_settings(requires_settings)
    if requires_gcp_adc and not _gcp_adc_available():
        # Surface the failed probe as a synthetic missing identifier so the
        # warning output names the failing dependency. Avoids overlapping
        # with any real env var.
        missing.append("GCP_APPLICATION_DEFAULT_CREDENTIALS")

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
            if first_tool is None:
                # Every instance is gated and creds are missing — the
                # tool would register nowhere. Crash loudly so a future
                # ``_INSTANCES`` shrink (phase-3 / phase-4) cannot
                # silently de-tooth a tool. Returning a naked ``fn``
                # would also break ``module.tool_name.fn`` access.
                raise RuntimeError(
                    f"shared_tool {tool_name!r} would register on no MCP server: "
                    f"every instance is gated and required settings are missing "
                    f"({missing}). Either populate the settings or remove the "
                    f"requires_settings gate."
                )
            return first_tool

        for instance in _INSTANCES:
            tool_obj = instance.tool(*args, **kwargs)(fn)
            if first_tool is None:
                first_tool = tool_obj
        if first_tool is None:
            # ``_INSTANCES`` is empty — likely a misconfiguration during
            # phase-3 lift-and-shift. Same loud failure as above.
            raise RuntimeError(
                f"shared_tool {tool_name!r} has no MCP instance to register on "
                f"(_INSTANCES is empty); aborting at startup."
            )
        return first_tool

    return decorator
