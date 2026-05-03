"""Unified per-request authentication context for MCP servers.

A single :class:`RequestContext` is populated by the auth middleware of each
MCP mount (harness, agcouch, future external mounts) and read by tools via
:func:`current_request_context` / :func:`require_caller_user_id`.

The previous ``dict[str, Any]`` shape and the per-field accessor functions
(``get_authenticated_user_email`` / ``get_requesting_user_id`` /
``get_ahs_agent_name`` / ``get_ahs_session_id``) are gone — tools now read
``ctx.requesting_user_id`` (or call :func:`require_caller_user_id`) directly.

Email is **never** used for identity at the tool layer. The OAuth middleware
resolves the verified email to a user_id once at ``verify_token`` time and
stores it in ``audit_email`` purely for ``MCPAuditLog`` rendering.

This module lives in ``ypl/mcp_common/`` so both the agcouch MCP server
(``ypl/mcp_server``) and the harness MCP server
(``ypl/agent_harness_service``) can import it without violating the
architecture-test layering. ``ypl/mcp_common/`` must not import from either
of those packages.
"""

from __future__ import annotations
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

#: One of the three trust models a request can carry:
#:
#: - ``agent_secret``: harness MCP — the AHS runner (or Codex CLI) sent
#:   ``AHS_MCP_SECRET``. The runner injects ``X-User-ID`` /
#:   ``X-AHS-Agent-Name`` / ``X-AHS-Session-ID`` into the sandboxed
#:   ``.mcp.json`` and the agent cannot tamper with them.
#: - ``oauth_user``: agcouch MCP — the request carried a Google OAuth bearer
#:   token whose email is in ``ALLOWED_MCP_EMAIL_DOMAINS``. The OAuth
#:   provider resolves the email to a ``user_id`` once at verify time.
#:   (The legacy ``yupp_dev_*`` token path also lands here during the
#:   phase-5 deprecation window — same shape, ``audit_email`` carries the
#:   token's email, ``requesting_user_id`` is resolved at middleware time.)
#: - ``proxy``: reserved for future MCP mounts behind a trusted proxy
#:   (e.g. Runlayer) that forwards an already-verified user identity.
AuthKind = Literal["agent_secret", "oauth_user", "proxy"]


@dataclass(frozen=True)
class RequestContext:
    """Per-request identity, threaded through the MCP middleware stack.

    A single instance describes "who is calling this tool, and how do we
    know" for the lifetime of one MCP request. Constructed by the auth
    middleware of each MCP mount; read by tools via
    :func:`current_request_context` (or, when an authenticated identity is
    required, :func:`require_caller_user_id`).

    Attributes:
        auth_kind: How the caller's identity was established. Tools rarely
            care about this directly — the auth middleware has already
            applied the trust model to populate the remaining fields.
        requesting_user_id: The platform ``user_id`` (UUID string) the
            caller is acting as. ``None`` is allowed only for internal
            agent_secret callers that did not stamp ``X-User-ID``; tools
            that need an attributable user must call
            :func:`require_caller_user_id`.
        ahs_session_id: The AHS session UUID the caller is running inside.
            Tamper-proof for ``agent_secret`` (set from ``X-AHS-Session-ID``
            injected by the runner). Usually ``None`` for ``oauth_user``.
        ahs_agent_name: The AHS agent name (e.g. ``eng-raccoon``).
            Tamper-proof for ``agent_secret``. Usually ``None`` for
            ``oauth_user``.
        audit_email: The OAuth-verified email (or DevToken email during the
            phase-5 deprecation window). **Audit logging only** —
            persisted in ``MCPAuditLog.email`` so security review can
            attribute calls to a human. Tool code MUST NOT use this for
            identity decisions.
        ip_address: Best-effort source IP for audit logging.
        user_agent: Source user-agent string for audit logging.
    """

    auth_kind: AuthKind
    requesting_user_id: str | None
    ahs_session_id: str | None = None
    ahs_agent_name: str | None = None
    audit_email: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None


# ---------------------------------------------------------------------------
# Per-asyncio-task ContextVars
# ---------------------------------------------------------------------------

#: The active :class:`RequestContext` for the current asyncio task / HTTP
#: request. Set by the MCP auth middleware (harness, agcouch, future
#: external mounts) on the way in and reset on the way out.
#:
#: Outside of an active MCP request — module load, background tasks not
#: spawned from a request, etc. — this var is ``None``. Tools should
#: typically use :func:`require_caller_user_id`, which raises a
#: tool-friendly ``PermissionError`` instead of returning ``None``.
request_context: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)

#: AHS session UUID set by the harness auth middleware from
#: ``X-AHS-Session-ID``. Mirrors :attr:`RequestContext.ahs_session_id` for
#: legacy callers (e.g. ``new_task`` enforcing ``allowed_subagents``) that
#: predate the typed context. New code should read
#: ``current_request_context().ahs_session_id`` instead.
mcp_session_id_var: ContextVar[str] = ContextVar("mcp_session_id", default="")


# ---------------------------------------------------------------------------
# Tool-facing accessors
# ---------------------------------------------------------------------------


def current_request_context() -> RequestContext | None:
    """Return the :class:`RequestContext` for the current MCP request.

    ``None`` outside of an active MCP request — module-load, background
    tasks not derived from a request, or unit tests that haven't set a
    context. Tools that require an authenticated identity should call
    :func:`require_caller_user_id` instead.
    """
    return request_context.get()


def require_caller_user_id() -> str:
    """Return the caller's ``user_id`` or raise ``PermissionError``.

    The single replacement for the old ``get_authenticated_user_email() /
    if auth_email == "unknown"`` boilerplate. Tools that need an
    attributable user call this once at the top and pass the result
    downstream.

    Raises:
        PermissionError: when no :class:`RequestContext` is set, or the
            context's ``requesting_user_id`` is ``None`` (e.g. an internal
            ``agent_secret`` caller that didn't stamp ``X-User-ID`` and
            therefore cannot be attributed to a real user).
    """
    ctx = request_context.get()
    if ctx is None or ctx.requesting_user_id is None:
        raise PermissionError("Authentication required")
    return ctx.requesting_user_id
