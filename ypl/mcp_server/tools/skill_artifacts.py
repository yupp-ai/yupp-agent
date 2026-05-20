"""MCP tools for agent SKILL artifacts — scope-aware wrappers.

SKILL artifacts are inline-stored, scope-qualified markdown documents that
the system-prompt builder merges into the per-session skill catalog. They
share their storage shape with MEMORY artifacts but add structured
frontmatter (``name`` / ``description`` / ``trigger_keywords``) so the
catalog merger can describe each entry without reading the body.

Three tools live here:

- :func:`save_skill`   — write a new SKILL (or new version under an
                         existing slug) for the caller's scope.
- :func:`load_skill_artifact` — read a SKILL by name. Used as the DB
                         fallback for the disk-based ``load_skill``
                         (declared in ``ypl/agent_harness_service/tools/skills.py``).
- :func:`search_skills` — substring search over title / description /
                          inline content, scoped to caller visibility.

Authorization mirrors MEMORY exactly: user/agent-scoped writes must
target the caller's own identity; ``topic`` is shared. Reads are
constrained by the caller's visibility (own user + own agent + topic).
"""

from typing import Any

from ypl.agent_harness_service.artifact_store import (
    ArtifactError,
    create_artifact,
    get_artifact_by_slug,
    read_artifact_content,
)
from ypl.agent_harness_service.artifact_store import (
    search_artifacts as _search_artifacts,
)
from ypl.agent_harness_service.memory_store import (
    VALID_MEMORY_SCOPES,
    caller_can_write_memory,
    validate_memory_scope_shape,
)
from ypl.agent_harness_service.skill_store import (
    build_skill_metadata,
    parse_skill_frontmatter,
)
from ypl.db.agent_harness import AgentArtifactType
from ypl.mcp_common.shared_tool import shared_tool
from ypl.mcp_server.tools.agent_artifacts import _resolve_caller_context
from ypl.mcp_server.tools.artifact_notifier import notify_artifact_event
from ypl.mcp_server.tools.memory_artifacts import (
    _default_subject_for_scope,
    _display_memory_address,
    _memory_caller_from_context,
)
from ypl.structured_logger import get_logger

logger = get_logger()


@shared_tool()
async def save_skill(
    name: str,
    content: str,
    description: str | None = None,
    scope: str = "agent",
    subject: str | None = None,
) -> dict[str, Any]:
    """Save (or append a new version of) a SKILL artifact for the caller.

    Skills are scoped exactly like memories: the default scope is ``agent``
    — visible only to sessions running as the same agent. ``user`` skills
    are visible only to the calling user; ``topic`` skills are shared.

    The ``content`` is markdown; if it starts with a ``---``-delimited
    YAML frontmatter block, the ``name`` / ``description`` /
    ``trigger_keywords`` fields are parsed out and persisted into
    ``artifact_metadata->'skill'`` so the system-prompt catalog merger
    can list this skill without reading its body. The MCP-tool ``name``
    argument always wins over any ``name:`` in the frontmatter (it's the
    slug the skill is registered under).

    Each save allocates a new version under the (scope, subject, slug)
    sequence; the latest version is what ``load_skill`` / catalog merge
    returns. Slug rules match the existing artifact slug pattern
    (``[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}``).

    Parameters:
        name: Skill slug — also the catalog name agents see in their
            system-prompt skill list.
        content: Markdown body (stored inline). Leading YAML frontmatter
            is parsed; everything else is preserved verbatim.
        description: Optional one-line summary stored on the row. Falls
            back to the parsed frontmatter ``description`` if not given.
        scope: ``agent`` (default) | ``user`` | ``topic``.
        subject: Override subject — must match caller identity for
            user/agent scopes. Omit to use the caller's own identity.

    Returns:
        On success: ``{ success: True, artifact_id, address, scope,
        subject, slug, version, message }``
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

    frontmatter = parse_skill_frontmatter(content)
    # Lift description from frontmatter when caller didn't pass one explicitly —
    # mirrors the disk-skill behaviour where the catalog reads description
    # straight from the YAML block.
    resolved_description = description if description is not None else frontmatter.description
    extra_metadata = build_skill_metadata(frontmatter, override_name=name)

    session_id, _user_id, agent_id = await _resolve_caller_context()

    # Versioning mirrors save_memory: if the (scope, subject, slug) has an
    # existing version, append; otherwise start at 1.
    try:
        existing = await get_artifact_by_slug(
            name,
            artifact_type=AgentArtifactType.SKILL,
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
            title=name,
            description=resolved_description,
            creator_user_id=caller.user_id,
            creator_agent_id=agent_id,
            agent_session_id=session_id,
            named_slug=name,
            create_new_slug=create_new,
            artifact_type=AgentArtifactType.SKILL,
            memory_scope=scope,
            memory_scope_subject=effective_subject,
            extra_metadata=extra_metadata,
        )
    except ArtifactError as exc:
        return {"success": False, "error": str(exc)}
    except Exception:
        logger.exception("Failed to save skill", name=name, scope=scope)
        return {"success": False, "error": "Internal error saving skill."}

    address = _display_memory_address(scope, effective_subject, name)
    logger.info(
        "Skill saved",
        artifact_id=str(artifact.agent_artifact_id),
        address=address,
        version=artifact.version,
    )

    # Notify Slack (fire-and-forget; never blocks the tool).
    await notify_artifact_event(
        artifact=artifact,
        event="created" if create_new else "new_version",
        agent_name=caller.agent_name,
        session_id=session_id,
        user_id=caller.user_id,
    )

    return {
        "success": True,
        "artifact_id": str(artifact.agent_artifact_id),
        "address": address,
        "scope": scope,
        "subject": effective_subject,
        "slug": name,
        "version": artifact.version,
        "message": f"Skill '{address}' saved (version {artifact.version}).",
    }


@shared_tool()
async def load_skill_artifact(
    name: str,
    scope: str | None = None,
    subject: str | None = None,
    version: int | None = None,
) -> dict[str, Any]:
    """Load a SKILL artifact's body by name.

    Used by agents (and by the disk-based ``load_skill`` tool as a
    fallback) to fetch the full markdown body of a saved skill. If
    ``scope`` is unset, the caller's full visibility (topic + own user
    + own agent) is searched and the first match wins, with priority
    ``agent`` > ``user`` > ``topic`` so per-agent customisations
    shadow shared skills.

    Parameters:
        name: Skill slug to load.
        scope: Optional narrowing — ``agent`` | ``user`` | ``topic``.
        subject: Override subject — must fall in the caller's
            visibility. Omit to use the caller's own identity.
        version: Specific version to read; omit for the latest
            non-archived.

    Returns:
        On success: ``{ success: True, artifact_id, address, scope,
        subject, slug, version, content, content_type, description }``
        On not-found / not-visible: ``{ success: False, error }``
    """
    caller = _memory_caller_from_context()

    # Explicit scope path: behave like load_memory.
    if scope is not None:
        if scope not in VALID_MEMORY_SCOPES:
            return {
                "success": False,
                "error": f"Invalid scope {scope!r}; allowed: {sorted(VALID_MEMORY_SCOPES)}",
            }
        effective_subject = subject if subject is not None else _default_subject_for_scope(scope, caller)
        try:
            validate_memory_scope_shape(scope, effective_subject)
        except ArtifactError as exc:
            return {"success": False, "error": str(exc)}
        # Cross-scope reads are rejected the same way memories are.
        if scope == "user" and caller.user_id is not None and effective_subject != caller.user_id:
            return {"success": False, "error": "Cross-user read rejected."}
        if scope == "agent" and caller.agent_name is not None and effective_subject != caller.agent_name:
            return {"success": False, "error": "Cross-agent read rejected."}
        scopes_to_try = [(scope, effective_subject)]
    else:
        # Implicit fallback path: agent > user > topic.
        scopes_to_try = []
        if caller.agent_name is not None:
            scopes_to_try.append(("agent", caller.agent_name))
        if caller.user_id is not None:
            scopes_to_try.append(("user", caller.user_id))
        scopes_to_try.append(("topic", None))

    artifact = None
    found_scope: str | None = None
    found_subject: str | None = None
    for candidate_scope, candidate_subject in scopes_to_try:
        try:
            artifact = await get_artifact_by_slug(
                name,
                version=version,
                artifact_type=AgentArtifactType.SKILL,
                memory_scope=candidate_scope,
                memory_scope_subject=candidate_subject,
            )
        except ArtifactError as exc:
            return {"success": False, "error": str(exc)}
        if artifact is not None:
            found_scope = candidate_scope
            found_subject = candidate_subject
            break

    if artifact is None:
        return {
            "success": False,
            "error": f"Skill {name!r} not found in the caller's visibility.",
        }

    try:
        data, content_type = await read_artifact_content(artifact)
    except ArtifactError as exc:
        return {"success": False, "error": str(exc)}
    except Exception:
        logger.exception("Failed to read skill body", name=name)
        return {"success": False, "error": "Internal error reading skill content."}

    return {
        "success": True,
        "artifact_id": str(artifact.agent_artifact_id),
        "address": _display_memory_address(found_scope, found_subject, name),
        "scope": found_scope,
        "subject": found_subject,
        "slug": name,
        "version": artifact.version,
        "content": data.decode("utf-8"),
        "content_type": content_type,
        "description": artifact.description,
    }


@shared_tool()
async def search_skills(
    query: str,
    scope: str | None = None,
    subject: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search SKILL artifacts visible to the caller.

    Substring-matches title, slug, description, and inline body. Rows
    outside the caller's visibility (topic + own user + own agent) are
    never returned.

    Parameters:
        query: Substring to match (case-insensitive, non-empty).
        scope: Optional narrowing — ``user`` | ``agent`` | ``topic``.
        subject: Optional subject narrowing (paired with scope).
        limit: Max results (default 20, cap 100).

    Returns:
        ``{ success, results: [...], count }``
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

    effective_subject = subject
    if scope in ("user", "agent") and effective_subject is None:
        effective_subject = _default_subject_for_scope(scope, caller)
    if scope is not None and subject is not None:
        if scope == "user" and caller.user_id is not None and subject != caller.user_id:
            return {"success": False, "error": "Cross-user search rejected."}
        if scope == "agent" and caller.agent_name is not None and subject != caller.agent_name:
            return {"success": False, "error": "Cross-agent search rejected."}

    try:
        artifacts = await _search_artifacts(
            query,
            artifact_type=AgentArtifactType.SKILL,
            limit=limit,
            memory_caller=caller if caller.has_identity else None,
            memory_scope=scope,
            memory_scope_subject=effective_subject,
        )
    except Exception:
        logger.exception("Failed to search skills", query=query, scope=scope)
        return {"success": False, "error": "Internal error searching skills."}

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
