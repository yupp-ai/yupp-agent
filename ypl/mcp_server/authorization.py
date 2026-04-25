"""Authorization helpers for MCP tools.

Two concepts:

- **Caller identity.** The ``user_id`` on whose behalf this request is being
  made. Resolved from the ``X-User-ID`` header when the middleware trusted it
  (see ``auth_dev_token.create_request_context``), otherwise from the MCP-
  authenticated email.

- **Ownership check.** A resource may be mutated if the caller is the creator
  OR the caller holds a resource-scoped admin permission
  (``MANAGE_AGENT_SCHEDULES``, ``MANAGE_AGENT_PROJECTS``,
  ``MANAGE_AGENT_SESSIONS``).

These helpers centralize that pattern so individual tools don't reinvent it.
"""

from __future__ import annotations

from ypl.backend.utils.soul_utils import has_permission_by_user_id_cached
from ypl.db.rbac import Permission
from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id_from_email
from ypl.mcp_server.core import get_requesting_user_id


async def resolve_caller_user_id(auth_email: str) -> tuple[str | None, str | None]:
    """Resolve the effective caller ``user_id`` for the current request.

    Prefers the ``X-User-ID`` header (which the middleware only populates
    when the token owner is authorized to assert user identity). Falls back
    to resolving the token's ``auth_email`` to a ``user_id``.

    Args:
        auth_email: The MCP-authenticated email (usually from
            ``get_authenticated_user_email()``).

    Returns:
        ``(caller_user_id, error)`` — on success ``error`` is ``None``. On
        failure ``caller_user_id`` is ``None`` and ``error`` describes the
        failure.
    """
    caller_user_id = get_requesting_user_id()
    if caller_user_id:
        return caller_user_id, None

    caller_user_id, err = await resolve_user_id_from_email(auth_email)
    if err:
        return None, err
    return caller_user_id, None


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
