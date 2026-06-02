"""Route-layer helpers for MEMORY artifacts.

Split out of ``artifact_routes.py`` to keep the MEMORY-specific HTTP
surface — caller-identity extraction, scope/subject query validation,
per-row read permission checks, slug-route scope resolution — in one
place. ``artifact_routes.py`` imports from here; every route that
accepts ``scope`` / ``subject`` query params or enforces MEMORY authz
routes through these helpers.

Nothing here owns a route definition; the routes themselves stay with
the artifact router so the URL tree keeps its single source of truth.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Query, Request

from ypl.agent_harness_service.memory_store import (
    VALID_MEMORY_SCOPES,
    MemoryCallerContext,
    caller_can_write_memory,
)
from ypl.db.agent_harness import SCOPED_INLINE_ARTIFACT_TYPES, AgentArtifact, AgentArtifactType

# ---------------------------------------------------------------------------
# Caller context dependency
# ---------------------------------------------------------------------------


def caller_context_from_request(request: Request) -> MemoryCallerContext:
    """Extract the memory caller context from trusted AHS headers.

    - ``X-User-ID`` — forwarded by the MCP server from the agent's session's
      ``requesting_user_id``.
    - ``X-AHS-Agent-Name`` — injected by the AHS runner into the session's
      MCP config; tamper-proof because the runner writes the config in a
      sandboxed workspace the agent can't modify.

    Missing headers produce ``None`` fields; a caller with neither header is
    treated as an admin (e.g. the Streamlit viewer, which reads with no user
    context) by the route handlers — see each route for exactly how that
    case is handled.
    """
    user_id = request.headers.get("X-User-ID")
    agent_name = request.headers.get("X-AHS-Agent-Name")
    return MemoryCallerContext(
        user_id=user_id or None,
        agent_name=agent_name or None,
    )


# Module-level Depends singleton so ruff B008 (no function calls in argument
# defaults) is happy while we still get FastAPI's per-request injection.
CALLER_CTX_DEP = Depends(caller_context_from_request)

MEMORY_SCOPE_QUERY = Query(
    None,
    alias="scope",
    description="Memory scope filter (MEMORY artifacts only): 'user', 'agent', or 'topic'.",
)
MEMORY_SUBJECT_QUERY = Query(
    None,
    alias="subject",
    description="Memory subject filter (MEMORY artifacts only): user_id for scope=user, agent name for scope=agent.",
)


# ---------------------------------------------------------------------------
# Authorization helpers
# ---------------------------------------------------------------------------


def caller_can_read_memory(caller: MemoryCallerContext, scope: str | None, subject: str | None) -> bool:
    """Return True if the caller may read MEMORY rows in (scope, subject).

    Admin callers (no identity) can read anything. Identity-bearing callers
    are bound by :func:`memory_read_clause` — here we pre-check the scope/
    subject filter so an out-of-scope request returns 403 instead of an
    empty result set.

    ``subject=None`` is permitted for ``scope=user`` / ``scope=agent``
    because the route layer defaults the subject to the caller's own
    user_id / agent_name. The authz check runs again on the resolved
    subject inside ``resolve_memory_slug_scope``.
    """
    if not caller.has_identity:
        return True
    if scope is None:
        # No narrowing: the read filter will constrain to caller's visibility.
        return True
    if scope == "topic":
        # Topic reads are always allowed; subject must be absent.
        return subject is None
    if scope == "user":
        if subject is None:
            # Will be defaulted to caller.user_id; allowed iff caller has one.
            return caller.user_id is not None
        return subject == caller.user_id
    if scope == "agent":
        if subject is None:
            return caller.agent_name is not None
        return subject == caller.agent_name
    return False


def validate_scope_query(
    *,
    scope: str | None,
    subject: str | None,
    caller: MemoryCallerContext,
) -> None:
    """Validate and authorize MEMORY scope/subject query parameters.

    Raises HTTP 400 for shape errors and HTTP 403 for cross-scope reads.
    Callers that don't pass a scope param are left alone — the store layer
    will apply ``memory_read_clause`` using the caller context.
    """
    if scope is None and subject is None:
        return
    if scope is not None and scope not in VALID_MEMORY_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid memory_scope {scope!r}; allowed: {sorted(VALID_MEMORY_SCOPES)}",
        )
    if scope is None and subject is not None:
        raise HTTPException(status_code=400, detail="subject requires scope")
    # At this point scope is a valid string.
    assert scope is not None  # mypy
    if scope == "topic":
        if subject is not None:
            raise HTTPException(status_code=400, detail="scope=topic forbids subject")
    elif subject is None:
        # scope in {user, agent} without a subject: allow — route will apply
        # caller-driven narrowing (i.e., the caller's own subject).
        pass
    if not caller_can_read_memory(caller, scope, subject):
        raise HTTPException(
            status_code=403,
            detail=f"Caller not permitted to read memory_scope={scope!r}, subject={subject!r}",
        )


def enforce_memory_read_permission(artifact: AgentArtifact, caller: MemoryCallerContext) -> None:
    """Refuse reads of a MEMORY row that the caller has no right to see.

    Admin callers (no identity) bypass this check. For identity-bearing
    callers, the row is readable iff it's in the caller's visibility set
    (``topic`` ∪ user/self ∪ agent/self). We raise 404 (not 403) so we
    don't leak existence across user/agent scopes.
    """
    if artifact.artifact_type != AgentArtifactType.MEMORY:
        return
    if not caller.has_identity:
        return
    scope = artifact.memory_scope
    subject = artifact.memory_scope_subject
    if scope == "topic":
        return
    if scope == "user" and subject == caller.user_id:
        return
    if scope == "agent" and subject == caller.agent_name:
        return
    raise HTTPException(status_code=404, detail=f"Artifact {artifact.agent_artifact_id} not found")


def resolve_memory_slug_scope(
    *,
    artifact_type: AgentArtifactType,
    scope: str | None,
    subject: str | None,
    caller: MemoryCallerContext,
    require_write: bool = False,
) -> tuple[str | None, str | None]:
    """Resolve (scope, subject) for a MEMORY slug-based lookup.

    Returns the effective ``(scope, subject)`` to pass to the store helpers,
    applying caller-driven defaults:

    - scope=user or scope=agent without a subject → caller's own subject
      (common "my memories" path).
    - scope=topic → subject stays None.
    - scope missing on a MEMORY lookup → 400; MEMORY slugs are only
      addressable within a scope.

    Raises 400 / 403 as appropriate.
    """
    # MEMORY and SKILL are both scope+subject-addressable inline artifacts;
    # only unscoped types (TEXT, …) skip scope resolution. Gating on
    # SCOPED_INLINE_ARTIFACT_TYPES keeps SKILL slug lookups working instead of
    # falling through to ``(None, None)`` → a 400 in ``_scope_slug_filter``.
    if artifact_type not in SCOPED_INLINE_ARTIFACT_TYPES:
        return None, None
    if scope is None:
        raise HTTPException(
            status_code=400,
            detail=f"{artifact_type.value} slug routes require a 'scope' query parameter (user|agent|topic).",
        )
    if scope not in VALID_MEMORY_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid scope {scope!r}; allowed: {sorted(VALID_MEMORY_SCOPES)}",
        )
    if scope == "topic":
        if subject is not None:
            raise HTTPException(status_code=400, detail="scope=topic forbids subject")
        return "topic", None
    # scope in {user, agent}: default subject to caller's own.
    effective_subject = subject
    if effective_subject is None:
        effective_subject = caller.user_id if scope == "user" else caller.agent_name
    if not effective_subject:
        raise HTTPException(
            status_code=400,
            detail=f"scope={scope!r} requires a subject (or a caller identity to default from).",
        )
    # Authorization.
    if require_write:
        if not caller_can_write_memory(caller, scope, effective_subject):
            raise HTTPException(
                status_code=403,
                detail=f"Caller not permitted to write memory_scope={scope!r}, subject={effective_subject!r}",
            )
    else:
        if not caller_can_read_memory(caller, scope, effective_subject):
            raise HTTPException(
                status_code=403,
                detail=f"Caller not permitted to read memory_scope={scope!r}, subject={effective_subject!r}",
            )
    return scope, effective_subject
