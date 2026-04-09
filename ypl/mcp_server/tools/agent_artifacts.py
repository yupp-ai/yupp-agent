"""MCP tools for managing agent artifacts.

Agents call add_artifact when they produce a trackable output (PR, yuppaste,
report, etc.) and update_artifact to revise the record as the artifact evolves.
All writes are attributed to the calling agent's session via AHS headers.
"""

import asyncio
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError

from ypl.backend.db import get_async_session, get_async_session_read_replica, retry_db
from ypl.db.agent_harness import AgentArtifact, AgentArtifactType
from ypl.mcp_server.core import get_ahs_agent_name, get_ahs_session_id, get_requesting_user_id, mcp_server
from ypl.structured_logger import get_logger

logger = get_logger()

_VALID_TYPES = [t.value for t in AgentArtifactType]

# FK constraint name for the agent_session_id → agent_sessions reference.
# When the session row hasn't been committed yet (race condition between AHS
# session-start and the agent's first MCP call), the INSERT violates this
# constraint.  We retry with backoff before falling back to session_id=None.
_SESSION_FK_CONSTRAINT = "fk_agent_artifacts_agent_session_id_agent_sessions"
_SESSION_FK_MAX_RETRIES = 3
_SESSION_FK_RETRY_DELAYS = (0.5, 1.0, 2.0)  # seconds; one entry per retry
assert len(_SESSION_FK_RETRY_DELAYS) == _SESSION_FK_MAX_RETRIES, (
    "_SESSION_FK_RETRY_DELAYS must have exactly _SESSION_FK_MAX_RETRIES entries"
)


def _parse_session_id(raw: str | None) -> uuid.UUID | None:
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        logger.warning("Invalid X-AHS-Session-ID header value", raw=raw)
        return None


@retry_db
async def _create_artifact(
    *,
    artifact_type: AgentArtifactType,
    title: str,
    url: str,
    description: str | None,
    creator_user_id: str | None,
    creator_agent_id: uuid.UUID | None,
    agent_session_id: uuid.UUID | None,
    agent_task_id: uuid.UUID | None,
    artifact_metadata: dict[str, Any] | None,
) -> AgentArtifact:
    async with get_async_session() as session:
        artifact = AgentArtifact(
            artifact_type=artifact_type,
            title=title,
            url=url,
            description=description,
            creator_user_id=creator_user_id,
            creator_agent_id=creator_agent_id,
            agent_session_id=agent_session_id,
            agent_task_id=agent_task_id,
            artifact_metadata=artifact_metadata,
        )
        session.add(artifact)
        await session.commit()
        await session.refresh(artifact)
        return artifact


async def _create_artifact_with_session_retry(
    *,
    artifact_type: AgentArtifactType,
    title: str,
    url: str,
    description: str | None,
    creator_user_id: str | None,
    creator_agent_id: uuid.UUID | None,
    agent_session_id: uuid.UUID | None,
    agent_task_id: uuid.UUID | None,
    artifact_metadata: dict[str, Any] | None,
) -> tuple[AgentArtifact, bool]:
    """Create an artifact, retrying on session FK violations with backoff.

    The session row may not be committed by the time this tool is called —
    there is a race condition between ``session.flush()`` (which inserts the
    AgentSession row inside an open transaction) and the agent's first MCP call.
    Because ``IntegrityError`` is excluded from ``retry_db``, it won't be
    retried automatically; this function handles that case explicitly.

    Returns:
        (artifact, session_linked) — ``session_linked`` is False when the
        fallback path was used (artifact saved with ``agent_session_id=None``).
    """
    if agent_session_id is None:
        return await _create_artifact(
            artifact_type=artifact_type,
            title=title,
            url=url,
            description=description,
            creator_user_id=creator_user_id,
            creator_agent_id=creator_agent_id,
            agent_session_id=None,
            agent_task_id=agent_task_id,
            artifact_metadata=artifact_metadata,
        ), True

    last_exc: IntegrityError | None = None
    for attempt in range(_SESSION_FK_MAX_RETRIES + 1):
        try:
            artifact = await _create_artifact(
                artifact_type=artifact_type,
                title=title,
                url=url,
                description=description,
                creator_user_id=creator_user_id,
                creator_agent_id=creator_agent_id,
                agent_session_id=agent_session_id,
                agent_task_id=agent_task_id,
                artifact_metadata=artifact_metadata,
            )
            return artifact, True
        except IntegrityError as exc:
            if getattr(exc.orig, "constraint_name", None) != _SESSION_FK_CONSTRAINT:
                raise
            last_exc = exc
            if attempt < _SESSION_FK_MAX_RETRIES:
                delay = _SESSION_FK_RETRY_DELAYS[attempt]
                logger.warning(
                    "Session FK not found for add_artifact — retrying",
                    attempt=attempt + 1,
                    max_retries=_SESSION_FK_MAX_RETRIES,
                    session_id=str(agent_session_id),
                    retry_delay_s=delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.warning(
                    "Session FK not found for add_artifact — all retries exhausted, falling back to unlinked artifact",
                    attempt=attempt + 1,
                    max_retries=_SESSION_FK_MAX_RETRIES,
                    session_id=str(agent_session_id),
                )

    # All retries exhausted.  Save the artifact without the session link so
    # the artifact is never silently lost — the caller will log/warn about it.
    logger.error(
        "Session FK violation persists after retries — saving artifact without session link",
        session_id=str(agent_session_id),
        title=title,
        exc_info=last_exc,
    )
    artifact = await _create_artifact(
        artifact_type=artifact_type,
        title=title,
        url=url,
        description=description,
        creator_user_id=creator_user_id,
        creator_agent_id=creator_agent_id,
        agent_session_id=None,  # fallback: unlink from missing session
        agent_task_id=agent_task_id,
        artifact_metadata=artifact_metadata,
    )
    return artifact, False


@retry_db
async def _update_artifact(
    artifact_id: uuid.UUID,
    *,
    title: str | None,
    description: str | None,
    url: str | None,
    artifact_metadata: dict[str, Any] | None,
    merge_metadata: bool,
) -> AgentArtifact | None:
    async with get_async_session() as session:
        result = await session.get(AgentArtifact, artifact_id)
        if result is None or result.deleted_at is not None:
            return None
        # TODO: Add ownership check — verify result.agent_session_id matches the
        # caller's session before allowing updates, to prevent cross-session artifact
        # mutation. Requires threading session context through this helper.

        if title is not None:
            result.title = title
        if description is not None:
            result.description = description
        if url is not None:
            result.url = url
        if artifact_metadata is not None:
            if merge_metadata and result.artifact_metadata:
                merged = dict(result.artifact_metadata)
                merged.update(artifact_metadata)
                result.artifact_metadata = merged
            else:
                result.artifact_metadata = artifact_metadata

        session.add(result)
        await session.commit()
        await session.refresh(result)
        return result


@mcp_server.tool()
async def add_artifact(
    artifact_type: str,
    title: str,
    url: str,
    description: str | None = None,
    agent_task_id: str | None = None,
    artifact_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Register a new artifact in the agent artifact registry.

    Call this immediately after creating any trackable output — a yuppaste,
    pull request, investigation report, or other resource. This creates a
    persistent record tied to your current session so the artifact can be
    found, referenced, and tracked across sessions.

    Parameters:
        artifact_type: Classification of the artifact. Valid values:
            - YUPPASTE    — text snippet, report, or investigation paste (http://go/p/...)
            - CODE_REVIEW — pull request or code review artifact (GitHub PR URL)
            - OTHER       — anything else (describe in artifact_metadata)
        title: Short human-readable name for the artifact (e.g. "Fix auth bug PR").
        url: Canonical URL for the artifact (yuppaste go-link, GitHub PR URL, etc.).
        description: Optional one-line summary of what this artifact contains.
        agent_task_id: If this artifact was created as part of a project task, pass
            the task UUID here to link them.
        artifact_metadata: Optional key-value bag for type-specific data, e.g.
            {"pr_number": 123, "repo": "yupp-mind"} for CODE_REVIEW artifacts.

    Returns:
        { success, artifact_id, message } on success.
        { success: false, error } on failure.
    """
    if artifact_type not in _VALID_TYPES:
        return {
            "success": False,
            "error": f"Invalid artifact_type '{artifact_type}'. Must be one of: {_VALID_TYPES}",
        }

    parsed_type = AgentArtifactType(artifact_type)
    agent_session_id = _parse_session_id(get_ahs_session_id())
    requesting_user_id = get_requesting_user_id()

    # Resolve creator_agent_id from the agent name.
    agent_name = get_ahs_agent_name()
    creator_agent_id: uuid.UUID | None = None
    if agent_name:
        creator_agent_id = await _resolve_agent_id(agent_name)

    parsed_task_id: uuid.UUID | None = None
    if agent_task_id:
        try:
            parsed_task_id = uuid.UUID(agent_task_id)
        except ValueError:
            return {"success": False, "error": f"Invalid agent_task_id '{agent_task_id}': not a valid UUID"}

    try:
        artifact, session_linked = await _create_artifact_with_session_retry(
            artifact_type=parsed_type,
            title=title,
            url=url,
            description=description,
            creator_user_id=requesting_user_id,
            creator_agent_id=creator_agent_id,
            agent_session_id=agent_session_id,
            agent_task_id=parsed_task_id,
            artifact_metadata=artifact_metadata,
        )
    except Exception:
        logger.exception("Failed to store artifact", agent_name=agent_name, title=title)
        return {"success": False, "error": "Internal error storing artifact — the artifact was not saved."}

    logger.info(
        "Artifact registered",
        artifact_id=str(artifact.agent_artifact_id),
        artifact_type=artifact_type,
        title=title,
        agent_name=agent_name,
        session_id=str(agent_session_id),
        session_linked=session_linked,
    )

    message = (
        f"Artifact '{title}' ({artifact_type}) registered with ID {artifact.agent_artifact_id}. "
        "Use this ID to reference the artifact in future sessions or update it later."
    )
    if not session_linked:
        message += (
            " Note: the artifact could not be linked to the current session "
            f"(session {agent_session_id} was not found in the database after retries)."
        )

    return {
        "success": True,
        "artifact_id": str(artifact.agent_artifact_id),
        "message": message,
    }


@mcp_server.tool()
async def update_artifact(
    artifact_id: str,
    title: str | None = None,
    description: str | None = None,
    url: str | None = None,
    artifact_metadata: dict[str, Any] | None = None,
    merge_metadata: bool = True,
) -> dict[str, Any]:
    """Update an existing artifact record.

    Use this to revise the title, URL, description, or metadata of an artifact
    that was previously registered via add_artifact — for example, when a draft
    PR is promoted to ready-for-review, or when a yuppaste is superseded.

    Parameters:
        artifact_id: UUID of the artifact to update (returned by add_artifact).
        title: New title (omit to leave unchanged).
        description: New description (omit to leave unchanged).
        url: New canonical URL (omit to leave unchanged).
        artifact_metadata: New/additional metadata dict (omit to leave unchanged).
        merge_metadata: If True (default), merges artifact_metadata into the
            existing metadata. If False, replaces it entirely.

    Returns:
        { success, artifact_id, message } on success.
        { success: false, error } on failure or not-found.
    """
    try:
        parsed_id = uuid.UUID(artifact_id)
    except ValueError:
        return {"success": False, "error": f"Invalid artifact_id '{artifact_id}': not a valid UUID"}

    if all(v is None for v in [title, description, url, artifact_metadata]):
        return {
            "success": False,
            "error": "At least one field (title, description, url, artifact_metadata) must be provided",
        }

    try:
        artifact = await _update_artifact(
            parsed_id,
            title=title,
            description=description,
            url=url,
            artifact_metadata=artifact_metadata,
            merge_metadata=merge_metadata,
        )
    except Exception:
        logger.exception("Failed to update artifact", artifact_id=artifact_id)
        return {"success": False, "error": "Internal error updating artifact."}

    if artifact is None:
        return {"success": False, "error": f"Artifact '{artifact_id}' not found or has been deleted."}

    logger.info("Artifact updated", artifact_id=artifact_id, agent_name=get_ahs_agent_name())

    return {
        "success": True,
        "artifact_id": str(artifact.agent_artifact_id),
        "message": f"Artifact '{artifact.title}' updated successfully.",
    }


@mcp_server.tool()
async def list_artifacts(
    artifact_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List artifacts from the current session.

    Returns recent artifacts for the current agent session. Returns an empty
    list if the caller has no session context (no X-AHS-Session-ID header).
    Use artifact_type to filter to a specific kind.

    Parameters:
        artifact_type: Optional filter. One of: YUPPASTE, CODE_REVIEW, OTHER.
        limit: Maximum number of artifacts to return (default 20, max 100).

    Returns:
        { success, artifacts: [...], count } on success.
    """
    from sqlmodel import col, select

    from ypl.db.agent_harness import AgentArtifact

    limit = min(max(limit, 1), 100)
    agent_session_id = _parse_session_id(get_ahs_session_id())
    if agent_session_id is None:
        return {"success": True, "artifacts": [], "count": 0}

    parsed_type: AgentArtifactType | None = None
    if artifact_type:
        if artifact_type not in _VALID_TYPES:
            return {
                "success": False,
                "error": f"Invalid artifact_type '{artifact_type}'. Must be one of: {_VALID_TYPES}",
            }
        parsed_type = AgentArtifactType(artifact_type)

    try:
        async with get_async_session_read_replica() as session:
            stmt = (
                select(AgentArtifact)
                .where(AgentArtifact.deleted_at.is_(None))  # type: ignore[union-attr]
                .order_by(col(AgentArtifact.created_at).desc())
                .limit(limit)
            )
            if agent_session_id:
                stmt = stmt.where(AgentArtifact.agent_session_id == agent_session_id)
            if parsed_type is not None:
                stmt = stmt.where(AgentArtifact.artifact_type == parsed_type)

            result = await session.execute(stmt)
            artifacts = result.scalars().all()
    except Exception:
        logger.exception("Failed to list artifacts")
        return {"success": False, "error": "Internal error listing artifacts."}

    rows = [
        {
            "artifact_id": str(a.agent_artifact_id),
            "artifact_type": a.artifact_type.value,
            "title": a.title,
            "url": a.url,
            "description": a.description,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "artifact_metadata": a.artifact_metadata,
        }
        for a in artifacts
    ]

    return {"success": True, "artifacts": rows, "count": len(rows)}


# ============================================================================
# Internal helpers
# ============================================================================


@retry_db
async def _resolve_agent_id(agent_name: str) -> uuid.UUID | None:
    """Resolve an agent name to its UUID. Returns None if not found."""
    from sqlmodel import select

    from ypl.db.agent_harness import Agent

    async with get_async_session_read_replica() as session:
        result = await session.execute(
            select(Agent.agent_id).where(Agent.name == agent_name).where(Agent.deleted_at.is_(None))  # type: ignore[union-attr]
        )
        row = result.first()
        return row.agent_id if row else None
