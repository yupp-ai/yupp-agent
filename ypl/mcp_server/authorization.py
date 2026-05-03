"""Authorization helpers for MCP tools.

Two concepts:

- **Caller identity.** The ``user_id`` on whose behalf this request is being
  made. Populated once by the auth middleware (harness, agcouch OAuth, or
  agcouch DevToken) on the typed
  :class:`~ypl.mcp_common.auth_context.RequestContext`. Tools read it via
  :func:`ypl.mcp_common.auth_context.require_caller_user_id`; the legacy
  email-fallback layer is gone.

- **Ownership check.** A resource may be mutated if the caller is the
  creator OR holds a resource-scoped admin permission
  (``MANAGE_AGENT_SCHEDULES``, ``MANAGE_AGENT_PROJECTS``,
  ``MANAGE_AGENT_SESSIONS``).

These helpers centralize the ownership pattern so individual tools don't
reinvent it.
"""

from __future__ import annotations

from ypl.backend.utils.soul_utils import has_permission_by_user_id_cached
from ypl.db.rbac import Permission


async def ensure_owner_or_permission(
    caller_user_id: str,
    resource_owner_user_id: str,
    admin_permission: Permission,
) -> str | None:
    """Verify the caller may act on a resource owned by someone else.

    Allowed when either:
      1. The caller is the resource owner; or
      2. The caller holds ``admin_permission`` (e.g. ``MANAGE_AGENT_SCHEDULES``
         for a schedule, ``MANAGE_AGENT_PROJECTS`` for a project).

    Returns ``None`` when allowed, otherwise a short error message suitable
    for the tool's ``{"success": False, "error": ...}`` response.
    """
    if caller_user_id == resource_owner_user_id:
        return None
    if await has_permission_by_user_id_cached(caller_user_id, admin_permission):
        return None
    return f"Not authorized: you do not own this resource and lack {admin_permission.value} permission"
