"""MCP tools for agent MEMORY artifacts — scope-aware wrappers.

MEMORY artifacts unify the old "agent memory" subsystem (see the legacy
``agent_memory.py`` GCS-backed tools) under the artifact registry.
Content lives inline on the row; reads and writes are constrained by
``(scope, subject)``::

    scope='agent', subject=<agent_name>  — per-agent notebook (the default)
    scope='user',  subject=<user_id>     — per-user notes
    scope='topic', subject=None          — globally shared topics

The caller's identity is threaded from the typed
:class:`~ypl.mcp_common.auth_context.RequestContext` published by the auth
middleware (``X-User-ID`` → ``ctx.requesting_user_id``, ``X-AHS-Agent-Name``
→ ``ctx.ahs_agent_name``). For user/agent scopes we reject writes where
the resolved subject doesn't match the caller — an agent can't save
another agent's memory or another user's memory.

Split from ``agent_artifacts.py`` to keep that file focused on TEXT /
CODE_REVIEW / OTHER artifact lifecycle while this one owns the four
memory tools and their authz logic. The shared helpers
(``_resolve_caller_context``, ``_resolve_agent_id``) still live in
``agent_artifacts.py`` and are imported here.
"""

from typing import Any

from ypl.agent_harness_service.artifact_store import (
    ArtifactError,
    create_artifact,
    get_artifact_by_slug,
    normalize_artifact_labels,
    read_artifact_content,
)
from ypl.agent_harness_service.artifact_store import (
    list_artifacts as _list_artifacts,
)
from ypl.agent_harness_service.artifact_store import (
    search_artifacts as _search_artifacts,
)
from ypl.agent_harness_service.common.constants import get_session_dir
from ypl.agent_harness_service.memory_materialization import write_memory_file
from ypl.agent_harness_service.memory_store import (
    VALID_MEMORY_SCOPES,
    MemoryCallerContext,
    caller_can_write_memory,
    validate_memory_scope_shape,
)
from ypl.db.agent_harness import AgentArtifactType
from ypl.mcp_common.auth_context import current_request_context
from ypl.mcp_common.shared_tool import shared_tool
from ypl.mcp_server.tools.agent_artifacts import _resolve_caller_context
from ypl.mcp_server.tools.artifact_notifier import notify_artifact_event
from ypl.structured_logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Memory-specific helpers
# ---------------------------------------------------------------------------


def _default_subject_for_scope(scope: str, caller: MemoryCallerContext) -> str | None:
    """Pick the caller's own subject for user/agent scopes; None for topic."""
    if scope == "topic":
        return None
    if scope == "user":
        return caller.user_id
    if scope == "agent":
        return caller.agent_name
    return None


def _memory_caller_from_context() -> MemoryCallerContext:
    """Build a :class:`MemoryCallerContext` from the typed RequestContext."""
    ctx = current_request_context()
    return MemoryCallerContext(
        user_id=(ctx.requesting_user_id if ctx else None) or None,
        agent_name=(ctx.ahs_agent_name if ctx else None) or None,
    )


def _display_memory_address(scope: str | None, subject: str | None, slug: str | None) -> str:
    """Render the ``u:X:slug`` / ``a:Y:slug`` / ``t:slug`` display form."""
    if slug is None or scope is None:
        return slug or ""
    if scope == "topic":
        return f"t:{slug}"
    prefix = "u" if scope == "user" else "a"
    return f"{prefix}:{subject}:{slug}"


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@shared_tool()
async def save_memory(
    topic: str,
    content: str,
    scope: str = "agent",
    subject: str | None = None,
    description: str | None = None,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """Save (append a new version of) a MEMORY artifact for the caller.

    Memories are scoped. The default scope is ``agent`` — the caller's own
    notebook, visible only to sessions running as the same agent. Explicit
    scope is required for ``user`` and ``topic``. The subject (user_id or
    agent name) defaults to the caller's own identity — cross-user /
    cross-agent writes are rejected with a clear error.

    Each save allocates a new version under the (scope, subject, slug)
    sequence; the latest version is what subsequent loads return. The slug
    is the ``topic`` argument verbatim — use URL-safe identifiers
    (``[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}``).

    Parameters:
        topic: Slug / topic name, e.g. ``user_preferences``.
        content: Markdown body (stored inline).
        scope: ``agent`` (default) | ``user`` | ``topic``.
        subject: Override subject — must match caller identity for
            user/agent scopes. Omit to use the caller's own identity.
        description: Optional short description stored on the row.
        labels: Optional searchable labels for this memory artifact version.

    Returns:
        On success: ``{ success: True, artifact_id, address, scope, subject, slug, version, message }``
        On failure: ``{ success: False, error }``
    """
    if scope not in VALID_MEMORY_SCOPES:
        return {
            "success": False,
            "error": f"Invalid scope {scope!r}; allowed: {sorted(VALID_MEMORY_SCOPES)}",
        }

    caller = _memory_caller_from_context()
    effective_subject = subject if subject is not None else _default_subject_for_scope(scope, caller)

    try:
        validate_memory_scope_shape(scope, effective_subject)
    except ArtifactError as exc:
        return {"success": False, "error": str(exc)}

    if not caller_can_write_memory(caller, scope, effective_subject):
        return {
            "success": False,
            "error": (
                f"Cross-scope write rejected: caller (user_id={caller.user_id!r}, "
                f"agent_name={caller.agent_name!r}) cannot write scope={scope!r}, "
                f"subject={effective_subject!r}."
            ),
        }

    session_id, _user_id, agent_id = await _resolve_caller_context()
    try:
        normalized_labels = normalize_artifact_labels(labels)
    except ArtifactError as exc:
        return {"success": False, "error": str(exc)}

    # Versioning: check if the slug exists in this (scope, subject); if not, create fresh.
    try:
        existing = await get_artifact_by_slug(
            topic,
            artifact_type=AgentArtifactType.MEMORY,
            memory_scope=scope,
            memory_scope_subject=effective_subject,
        )
    except ArtifactError as exc:
        return {"success": False, "error": str(exc)}
    create_new = existing is None

    try:
        artifact = await create_artifact(
            inline_content=content,
            content_type="text/markdown",
            title=topic,
            description=description,
            creator_user_id=caller.user_id,
            creator_agent_id=agent_id,
            agent_session_id=session_id,
            named_slug=topic,
            create_new_slug=create_new,
            artifact_type=AgentArtifactType.MEMORY,
            memory_scope=scope,
            memory_scope_subject=effective_subject,
            extra_metadata={"labels": normalized_labels} if normalized_labels else None,
        )
    except ArtifactError as exc:
        return {"success": False, "error": str(exc)}
    except Exception:
        logger.exception("Failed to save memory", topic=topic, scope=scope)
        return {"success": False, "error": "Internal error saving memory."}

    address = _display_memory_address(scope, effective_subject, topic)
    logger.info(
        "Memory saved",
        artifact_id=str(artifact.agent_artifact_id),
        address=address,
        version=artifact.version,
    )
    # Fan out a Slack notification (fire-and-forget; never blocks the tool).
    # Body content is never echoed; the notifier redacts user-scope subjects.
    await notify_artifact_event(
        artifact=artifact,
        event="created" if create_new else "new_version",
        agent_name=caller.agent_name,
        session_id=session_id,
        user_id=caller.user_id,
    )

    # Write-through: refresh the on-disk working copy in the calling
    # session's sandbox so the agent's in-turn ``cat`` / ``grep`` see
    # the new content. Strictly best-effort — the DB is authoritative
    # and the next session will re-materialize from the DB. A failure
    # here is logged but does NOT fail the save.
    ctx_for_session = current_request_context()
    session_id_str = ctx_for_session.ahs_session_id if ctx_for_session else None
    if session_id_str:
        try:
            workspace = get_session_dir(session_id_str)
            written_path = write_memory_file(workspace, scope, topic, content)
            if written_path is not None:
                logger.debug(
                    "Wrote memory file to sandbox (write-through)",
                    workspace=workspace,
                    path=written_path,
                )
        except Exception:
            logger.warning(
                "Memory write-through to sandbox failed (DB write succeeded)",
                session_id=session_id_str,
                slug=topic,
                scope=scope,
                exc_info=True,
            )

    return {
        "success": True,
        "artifact_id": str(artifact.agent_artifact_id),
        "address": address,
        "scope": scope,
        "subject": effective_subject,
        "slug": topic,
        "version": artifact.version,
        "labels": (artifact.artifact_metadata or {}).get("labels", []),
        "message": f"Memory '{address}' saved (version {artifact.version}).",
    }


@shared_tool()
async def load_memory(
    topic: str,
    scope: str = "agent",
    subject: str | None = None,
    version: int | None = None,
) -> dict[str, Any]:
    """Load the latest (or a specific) version of a MEMORY artifact.

    Subject defaults to the caller's own identity for user/agent scopes.
    Reads of other users' / other agents' memory are rejected (the artifact
    appears not to exist).

    Parameters:
        topic: Slug / topic name.
        scope: ``agent`` (default) | ``user`` | ``topic``.
        subject: Override subject — must fall in the caller's visibility.
        version: Specific version to read; omit for the latest non-archived.

    Returns:
        On success: ``{ success: True, address, scope, subject, slug, version,
        content, content_type, artifact_id, description }``
        On not-found / not-visible: ``{ success: False, error }``
    """
    if scope not in VALID_MEMORY_SCOPES:
        return {
            "success": False,
            "error": f"Invalid scope {scope!r}; allowed: {sorted(VALID_MEMORY_SCOPES)}",
        }
    caller = _memory_caller_from_context()
    effective_subject = subject if subject is not None else _default_subject_for_scope(scope, caller)

    try:
        validate_memory_scope_shape(scope, effective_subject)
    except ArtifactError as exc:
        return {"success": False, "error": str(exc)}

    # Readability check: reject cross-scope reads the same way the REST layer does.
    if scope == "user" and caller.user_id is not None and effective_subject != caller.user_id:
        return {"success": False, "error": "Cross-user read rejected."}
    if scope == "agent" and caller.agent_name is not None and effective_subject != caller.agent_name:
        return {"success": False, "error": "Cross-agent read rejected."}

    try:
        artifact = await get_artifact_by_slug(
            topic,
            version=version,
            artifact_type=AgentArtifactType.MEMORY,
            memory_scope=scope,
            memory_scope_subject=effective_subject,
        )
    except ArtifactError as exc:
        return {"success": False, "error": str(exc)}

    if artifact is None:
        return {
            "success": False,
            "error": f"Memory {_display_memory_address(scope, effective_subject, topic)!r} not found.",
        }

    try:
        data, content_type = await read_artifact_content(artifact)
    except ArtifactError as exc:
        return {"success": False, "error": str(exc)}
    except Exception:
        logger.exception("Failed to load memory content", topic=topic, scope=scope)
        return {"success": False, "error": "Internal error loading memory content."}

    return {
        "success": True,
        "artifact_id": str(artifact.agent_artifact_id),
        "address": _display_memory_address(scope, effective_subject, topic),
        "scope": scope,
        "subject": effective_subject,
        "slug": topic,
        "version": artifact.version,
        "content": data.decode("utf-8"),
        "content_type": content_type,
        "description": artifact.description,
    }


@shared_tool()
async def search_memory(
    query: str,
    scope: str | None = None,
    subject: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search MEMORY artifacts visible to the caller.

    Substring-matches title, slug, description, and inline content. MEMORY
    rows outside the caller's visibility (``topic`` + own user + own agent)
    are never returned.

    Parameters:
        query: Substring to match (case-insensitive, non-empty).
        scope: Optional narrowing — ``user`` | ``agent`` | ``topic``.
            When omitted the caller's full visibility is searched.
        subject: Optional subject narrowing (paired with scope). Cross-scope
            requests are rejected.
        limit: Max results (default 20, cap 100).

    Returns:
        ``{ success, results: [ {address, scope, subject, slug, version,
        title, description, artifact_id, created_at} ], count }``
    """
    if not query or not query.strip():
        return {"success": False, "error": "query must be non-empty."}
    if scope is not None and scope not in VALID_MEMORY_SCOPES:
        return {
            "success": False,
            "error": f"Invalid scope {scope!r}; allowed: {sorted(VALID_MEMORY_SCOPES)}",
        }
    limit = min(max(limit, 1), 100)

    caller = _memory_caller_from_context()

    # If a subject narrowing was requested, authorize it first.
    effective_subject = subject
    if scope in ("user", "agent") and effective_subject is None:
        effective_subject = _default_subject_for_scope(scope, caller)
    if scope is not None and subject is not None:
        # Explicit subject must match caller identity for user/agent scopes.
        if scope == "user" and caller.user_id is not None and subject != caller.user_id:
            return {"success": False, "error": "Cross-user search rejected."}
        if scope == "agent" and caller.agent_name is not None and subject != caller.agent_name:
            return {"success": False, "error": "Cross-agent search rejected."}

    try:
        artifacts = await _search_artifacts(
            query,
            artifact_type=AgentArtifactType.MEMORY,
            limit=limit,
            memory_caller=caller if caller.has_identity else None,
            memory_scope=scope,
            memory_scope_subject=effective_subject,
        )
    except Exception:
        logger.exception("Failed to search memory", query=query, scope=scope)
        return {"success": False, "error": "Internal error searching memory."}

    results = [
        {
            "artifact_id": str(a.agent_artifact_id),
            "address": _display_memory_address(a.memory_scope, a.memory_scope_subject, a.named_slug),
            "scope": a.memory_scope,
            "subject": a.memory_scope_subject,
            "slug": a.named_slug,
            "version": a.version,
            "title": a.title,
            "description": a.description,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in artifacts
    ]
    return {"success": True, "results": results, "count": len(results)}


@shared_tool()
async def list_memory(
    scope: str | None = None,
    subject: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List MEMORY artifacts visible to the caller.

    Defaults to returning everything in the caller's visibility (topic +
    own user + own agent). Narrow with ``scope`` / ``subject``; cross-scope
    requests are rejected.

    Parameters:
        scope: Optional narrowing — ``user`` | ``agent`` | ``topic``.
        subject: Optional subject narrowing. Paired with scope.
        limit: Max rows (default 50, cap 200).

    Returns:
        ``{ success, memories: [...], count }``
    """
    if scope is not None and scope not in VALID_MEMORY_SCOPES:
        return {
            "success": False,
            "error": f"Invalid scope {scope!r}; allowed: {sorted(VALID_MEMORY_SCOPES)}",
        }
    limit = min(max(limit, 1), 200)
    caller = _memory_caller_from_context()
    effective_subject = subject
    if scope in ("user", "agent") and effective_subject is None:
        effective_subject = _default_subject_for_scope(scope, caller)
    if scope is not None and subject is not None:
        if scope == "user" and caller.user_id is not None and subject != caller.user_id:
            return {"success": False, "error": "Cross-user list rejected."}
        if scope == "agent" and caller.agent_name is not None and subject != caller.agent_name:
            return {"success": False, "error": "Cross-agent list rejected."}

    try:
        artifacts = await _list_artifacts(
            artifact_type=AgentArtifactType.MEMORY,
            limit=limit,
            memory_caller=caller if caller.has_identity else None,
            memory_scope=scope,
            memory_scope_subject=effective_subject,
        )
    except Exception:
        logger.exception("Failed to list memory", scope=scope)
        return {"success": False, "error": "Internal error listing memory."}

    rows = [
        {
            "artifact_id": str(a.agent_artifact_id),
            "address": _display_memory_address(a.memory_scope, a.memory_scope_subject, a.named_slug),
            "scope": a.memory_scope,
            "subject": a.memory_scope_subject,
            "slug": a.named_slug,
            "version": a.version,
            "title": a.title,
            "description": a.description,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in artifacts
    ]
    return {"success": True, "memories": rows, "count": len(rows)}
