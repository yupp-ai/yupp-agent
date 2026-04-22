"""MCP tools for managing agent artifacts.

One artifact entity, three flavors:

- **TEXT** — textual artifact whose content is uploaded to the AHS blob store
  (markdown/plain/HTML). Use ``add_artifact`` with ``artifact_type="TEXT"``
  and a ``content`` body; the viewer URL is generated automatically and
  served from ``/ahs/artifacts/{uuid}``. Supports slug-based versioning,
  attachments, and in-place new-version updates via ``update_artifact_content``.
- **CODE_REVIEW** — pointer to a GitHub PR or code review. Use
  ``add_artifact`` with ``artifact_type="CODE_REVIEW"`` and a ``url``.
  No content is stored locally.
- **OTHER** — pointer to any external resource (doc, dashboard, link).
  Use ``add_artifact`` with ``artifact_type="OTHER"`` and a ``url``.

Both pointer flavors accept ``artifact_metadata`` for type-specific data
(e.g. ``{"pr_number": 123}`` for CODE_REVIEW).

All writes are attributed to the calling agent's session via AHS headers.
"""

import asyncio
import base64
import binascii
import json
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError

from ypl.agent_harness_service.artifact_store import (
    ArtifactError,
    Attachment,
    archive_artifact,
    archive_artifacts_by_slug,
    create_artifact,
    get_artifact_by_id,
    get_artifact_by_slug,
    read_artifact_content,
)
from ypl.agent_harness_service.artifact_store import (
    list_artifact_versions as _list_artifact_versions,
)
from ypl.agent_harness_service.artifact_store import (
    search_artifacts as _search_artifacts,
)
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
async def _insert_pointer_artifact(
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
    """Insert a pointer-only artifact row (no blob upload)."""
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


async def _insert_pointer_with_session_retry(
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
    """Insert a pointer artifact, retrying on session FK violations.

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
        return await _insert_pointer_artifact(
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
            artifact = await _insert_pointer_artifact(
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
    artifact = await _insert_pointer_artifact(
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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


def _decode_attachments_arg(raw: str | None) -> list[Attachment]:
    """Decode the MCP ``attachments`` JSON-string argument into Attachment objects.

    Shape::

        [{"filename": "image.png", "content_base64": "...", "content_type": "image/png"}, ...]
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"Invalid JSON for attachments: {exc}") from exc
    if not isinstance(parsed, list):
        raise ArtifactError("attachments must be a JSON array")
    result: list[Attachment] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ArtifactError(f"attachments[{i}] must be an object")
        try:
            data = base64.b64decode(item["content_base64"])
        except (KeyError, binascii.Error, ValueError) as exc:
            raise ArtifactError(f"attachments[{i}]: invalid content_base64") from exc
        result.append(
            Attachment(
                filename=str(item.get("filename", f"attachment-{i}")),
                data=data,
                content_type=str(item.get("content_type", "application/octet-stream")),
            )
        )
    return result


async def _resolve_caller_context() -> tuple[uuid.UUID | None, str | None, uuid.UUID | None]:
    """Return (session_id, requesting_user_id, creator_agent_id) from MCP headers."""
    session_id = _parse_session_id(get_ahs_session_id())
    user_id = get_requesting_user_id()
    agent_name = get_ahs_agent_name()
    agent_id = await _resolve_agent_id(agent_name) if agent_name else None
    return session_id, user_id, agent_id


# ---------------------------------------------------------------------------
# Unified add_artifact (TEXT with content, or CODE_REVIEW / OTHER as pointer)
# ---------------------------------------------------------------------------


@mcp_server.tool()
async def add_artifact(
    artifact_type: str,
    title: str,
    content: str | None = None,
    url: str | None = None,
    description: str | None = None,
    content_type: str = "text/markdown",
    named_slug: str | None = None,
    create_new_slug: bool = False,
    attachments: str | None = None,
    agent_task_id: str | None = None,
    artifact_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Register a new artifact in the artifact registry.

    One tool, three flavors — pick via ``artifact_type``:

    - ``TEXT``         — a textual artifact (markdown / plain / HTML). Pass
                         ``content``; a viewer URL is generated automatically.
                         Supports versioning via ``named_slug``.
    - ``CODE_REVIEW``  — pointer to a GitHub PR / code review. Pass ``url``.
    - ``OTHER``        — pointer to any other external resource. Pass ``url``.

    Parameters:
        artifact_type: TEXT, CODE_REVIEW, or OTHER.
        title: Short human-readable name (e.g. "Fix auth bug PR").
        content: TEXT only — the body of the artifact (markdown/plain/HTML).
            Ignored for CODE_REVIEW / OTHER.
        url: Pointer for CODE_REVIEW / OTHER (the PR URL, doc URL, etc.).
            Ignored for TEXT (the viewer URL is generated).
        description: Optional one-line summary of the artifact.
        content_type: TEXT only. One of text/markdown (default), text/plain, text/html.
        named_slug: TEXT only. URL-safe identifier for versioned artifacts.
            If set, pair with ``create_new_slug`` to control whether this is a
            brand-new slug or a new version of an existing one.
        create_new_slug: TEXT only. True = start a new slug at version 1;
            False (default) = append the next version onto an existing slug.
            Ignored when ``named_slug`` is not set.
        attachments: TEXT only. Optional JSON array of
            ``{filename, content_base64, content_type}`` — reference inline
            with ``![alt](attachment:<filename>)``.
        agent_task_id: Optional UUID of the project task this artifact belongs to.
        artifact_metadata: Optional key-value bag for type-specific data, e.g.
            ``{"pr_number": 123, "repo": "yupp-agent"}`` for CODE_REVIEW.

    Returns:
        On success (TEXT):       { success, artifact_id, url, title, slug, version, content_type, message }
        On success (pointer):    { success, artifact_id, url, title, message }
        On failure:              { success: false, error }
    """
    if artifact_type not in _VALID_TYPES:
        return {
            "success": False,
            "error": f"Invalid artifact_type '{artifact_type}'. Must be one of: {_VALID_TYPES}",
        }

    parsed_type = AgentArtifactType(artifact_type)

    parsed_task_id: uuid.UUID | None = None
    if agent_task_id:
        try:
            parsed_task_id = uuid.UUID(agent_task_id)
        except ValueError:
            return {"success": False, "error": f"Invalid agent_task_id '{agent_task_id}': not a valid UUID"}

    session_id, user_id, agent_id = await _resolve_caller_context()

    if parsed_type == AgentArtifactType.TEXT:
        if content is None:
            return {"success": False, "error": "TEXT artifacts require a 'content' body."}
        try:
            decoded_attachments = _decode_attachments_arg(attachments)
        except ArtifactError as exc:
            return {"success": False, "error": str(exc)}

        try:
            artifact = await create_artifact(
                content=content.encode("utf-8"),
                content_type=content_type,
                title=title,
                description=description,
                creator_user_id=user_id,
                creator_agent_id=agent_id,
                agent_session_id=session_id,
                agent_task_id=parsed_task_id,
                named_slug=named_slug,
                create_new_slug=create_new_slug,
                attachments=decoded_attachments,
                extra_metadata=artifact_metadata,
            )
        except ArtifactError as exc:
            return {"success": False, "error": str(exc)}
        except Exception:
            logger.exception("Failed to store TEXT artifact", title=title)
            return {"success": False, "error": "Internal error storing artifact."}

        logger.info(
            "Text artifact created",
            artifact_id=str(artifact.agent_artifact_id),
            title=title,
            slug=artifact.named_slug,
            version=artifact.version,
        )
        return {
            "success": True,
            "artifact_id": str(artifact.agent_artifact_id),
            "url": artifact.url,
            "title": artifact.title,
            "slug": artifact.named_slug,
            "version": artifact.version,
            "content_type": artifact.content_type,
            "message": f"Artifact '{title}' (TEXT) created at {artifact.url}.",
        }

    # CODE_REVIEW / OTHER: pointer-only flow.
    if url is None:
        return {"success": False, "error": f"{artifact_type} artifacts require a 'url'."}

    try:
        artifact, session_linked = await _insert_pointer_with_session_retry(
            artifact_type=parsed_type,
            title=title,
            url=url,
            description=description,
            creator_user_id=user_id,
            creator_agent_id=agent_id,
            agent_session_id=session_id,
            agent_task_id=parsed_task_id,
            artifact_metadata=artifact_metadata,
        )
    except Exception:
        logger.exception("Failed to store pointer artifact", title=title, artifact_type=artifact_type)
        return {"success": False, "error": "Internal error storing artifact — the artifact was not saved."}

    logger.info(
        "Pointer artifact registered",
        artifact_id=str(artifact.agent_artifact_id),
        artifact_type=artifact_type,
        title=title,
        session_linked=session_linked,
    )

    message = (
        f"Artifact '{title}' ({artifact_type}) registered with ID {artifact.agent_artifact_id}. "
        "Use this ID to reference the artifact in future sessions or update it later."
    )
    if not session_linked:
        message += (
            " Note: the artifact could not be linked to the current session "
            f"(session {session_id} was not found in the database after retries)."
        )

    return {
        "success": True,
        "artifact_id": str(artifact.agent_artifact_id),
        "url": artifact.url,
        "title": artifact.title,
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
    """Update the metadata of an existing artifact.

    Revise the title / URL / description / metadata of an artifact that was
    previously registered via ``add_artifact``. Does NOT change the content
    body of TEXT artifacts — use ``update_artifact_content`` for that (which
    creates a new version under the same slug).

    Typical uses:
    - Promote a CODE_REVIEW artifact from draft PR → ready-for-review (swap URL).
    - Correct a title or append metadata fields.

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
async def update_artifact_content(
    slug: str,
    content: str,
    title: str | None = None,
    description: str | None = None,
    content_type: str = "text/markdown",
    attachments: str | None = None,
) -> dict[str, Any]:
    """Save a new version of a TEXT artifact under an existing slug.

    Thin wrapper over ``add_artifact(artifact_type="TEXT", named_slug=slug,
    create_new_slug=False)``. Fails if ``slug`` has no prior versions — create
    the slug with ``add_artifact`` first (passing ``create_new_slug=True``).

    Parameters:
        slug: Existing named_slug to append a new version onto.
        content: New body of the artifact (markdown/plain/HTML).
        title: Optional title (defaults to the previous version's title
            shape if omitted — you almost always want to repeat it).
        description: Optional one-line summary for the new version.
        content_type: text/markdown (default), text/plain, or text/html.
        attachments: Optional JSON array, same shape as ``add_artifact``.

    Returns:
        { success, artifact_id, url, slug, version, content_type, message } on success.
        { success: false, error } on failure.
    """
    try:
        decoded_attachments = _decode_attachments_arg(attachments)
    except ArtifactError as exc:
        return {"success": False, "error": str(exc)}

    session_id, user_id, agent_id = await _resolve_caller_context()
    resolved_title = title if title is not None else slug

    try:
        artifact = await create_artifact(
            content=content.encode("utf-8"),
            content_type=content_type,
            title=resolved_title,
            description=description,
            creator_user_id=user_id,
            creator_agent_id=agent_id,
            agent_session_id=session_id,
            named_slug=slug,
            create_new_slug=False,
            attachments=decoded_attachments,
        )
    except ArtifactError as exc:
        return {"success": False, "error": str(exc)}
    except Exception:
        logger.exception("Failed to update artifact content", slug=slug)
        return {"success": False, "error": "Internal error updating artifact content."}

    logger.info(
        "Text artifact new version saved",
        artifact_id=str(artifact.agent_artifact_id),
        slug=slug,
        version=artifact.version,
    )

    return {
        "success": True,
        "artifact_id": str(artifact.agent_artifact_id),
        "url": artifact.url,
        "slug": artifact.named_slug,
        "version": artifact.version,
        "content_type": artifact.content_type,
        "message": f"Artifact '{slug}' updated to version {artifact.version} at {artifact.url}.",
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
        artifact_type: Optional filter. One of: TEXT, CODE_REVIEW, OTHER.
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
            "slug": a.named_slug,
            "version": a.version,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "artifact_metadata": a.artifact_metadata,
        }
        for a in artifacts
    ]

    return {"success": True, "artifacts": rows, "count": len(rows)}


@mcp_server.tool()
async def list_artifact_versions(slug: str) -> dict[str, Any]:
    """List every version of a TEXT artifact slug, oldest first.

    Archived versions are excluded. Returns an empty list if the slug has
    no active versions.

    Parameters:
        slug: The named_slug to enumerate versions for.

    Returns:
        { success, slug, versions: [{version, artifact_id, title, url, created_at}], count }
    """
    try:
        artifacts = await _list_artifact_versions(slug)
    except Exception:
        logger.exception("Failed to list artifact versions", slug=slug)
        return {"success": False, "error": "Internal error listing versions."}

    rows = [
        {
            "version": a.version,
            "artifact_id": str(a.agent_artifact_id),
            "title": a.title,
            "url": a.url,
            "content_type": a.content_type,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in artifacts
    ]
    return {"success": True, "slug": slug, "versions": rows, "count": len(rows)}


@mcp_server.tool()
async def search_artifacts(
    query: str,
    artifact_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Case-insensitive substring search over artifact title, description, slug, and attachment filenames.

    Parameters:
        query: Substring to match (required, non-empty).
        artifact_type: Optional filter. One of: TEXT, CODE_REVIEW, OTHER.
        limit: Max results (default 20, cap 100).
        offset: Pagination offset (default 0).
        include_archived: Include archived artifacts (default False).

    Returns:
        { success, results: [...], count }
    """
    if not query or not query.strip():
        return {"success": False, "error": "query must be non-empty."}

    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)

    parsed_type: AgentArtifactType | None = None
    if artifact_type:
        if artifact_type not in _VALID_TYPES:
            return {
                "success": False,
                "error": f"Invalid artifact_type '{artifact_type}'. Must be one of: {_VALID_TYPES}",
            }
        parsed_type = AgentArtifactType(artifact_type)

    try:
        artifacts = await _search_artifacts(
            query,
            artifact_type=parsed_type,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )
    except Exception:
        logger.exception("Failed to search artifacts", query=query)
        return {"success": False, "error": "Internal error searching artifacts."}

    rows = [
        {
            "artifact_id": str(a.agent_artifact_id),
            "artifact_type": a.artifact_type.value,
            "title": a.title,
            "url": a.url,
            "description": a.description,
            "slug": a.named_slug,
            "version": a.version,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in artifacts
    ]
    return {"success": True, "results": rows, "count": len(rows)}


@mcp_server.tool(
    name="read_artifact",
    description=(
        "Read a TEXT artifact's content + metadata. Pass ``id_or_slug`` as "
        "either a UUID or a named slug; when reading by slug, optionally pass "
        "``version`` to select a specific version (defaults to the latest "
        "non-archived). Returns an error for pointer artifacts "
        "(CODE_REVIEW / OTHER) — those have no stored body."
    ),
)
async def mcp_read_artifact(
    id_or_slug: str,
    version: int | None = None,
) -> dict[str, Any]:
    """Fetch a TEXT artifact's content + metadata."""
    artifact: AgentArtifact | None = None
    try:
        artifact_uuid = uuid.UUID(id_or_slug)
        artifact = await get_artifact_by_id(artifact_uuid)
    except ValueError:
        artifact = await get_artifact_by_slug(id_or_slug, version=version)

    if artifact is None:
        return {"error": f"Artifact not found: {id_or_slug!r}"}

    try:
        data, content_type = await read_artifact_content(artifact)
    except ArtifactError as exc:
        return {"error": str(exc)}
    except FileNotFoundError:
        return {"error": f"Artifact {artifact.agent_artifact_id} metadata exists but content is missing"}

    return {
        "artifact_id": str(artifact.agent_artifact_id),
        "url": artifact.url,
        "title": artifact.title,
        "description": artifact.description,
        "content": data.decode("utf-8"),
        "content_type": content_type,
        "slug": artifact.named_slug,
        "version": artifact.version,
        "attachments": (artifact.artifact_metadata or {}).get("attachments", []),
    }


@mcp_server.tool()
async def artifact_url(id_or_slug: str) -> dict[str, Any]:
    """Look up an artifact's canonical URL without fetching its content.

    Cheap way to get a shareable link for an artifact you know by ID or slug.
    Works for all artifact types — TEXT returns the viewer URL, CODE_REVIEW /
    OTHER return the stored pointer URL.

    Parameters:
        id_or_slug: Artifact UUID or named_slug.

    Returns:
        { success, artifact_id, url, title, artifact_type } on success.
        { success: false, error } if not found.
    """
    artifact: AgentArtifact | None = None
    try:
        artifact_uuid = uuid.UUID(id_or_slug)
        artifact = await get_artifact_by_id(artifact_uuid)
    except ValueError:
        artifact = await get_artifact_by_slug(id_or_slug)

    if artifact is None:
        return {"success": False, "error": f"Artifact not found: {id_or_slug!r}"}

    return {
        "success": True,
        "artifact_id": str(artifact.agent_artifact_id),
        "url": artifact.url,
        "title": artifact.title,
        "artifact_type": artifact.artifact_type.value,
    }


@mcp_server.tool(
    name="archive_artifact",
    description=(
        "Archive (soft-delete) a single artifact by UUID. The row stays in the "
        "DB with ``is_archived=true``; blobs are retained so the artifact can "
        "be un-archived later by a migration/admin if needed."
    ),
)
async def mcp_archive_artifact(artifact_id: str) -> dict[str, Any]:
    try:
        artifact_uuid = uuid.UUID(artifact_id)
    except ValueError:
        return {"error": f"Invalid artifact_id: {artifact_id!r}"}
    archived = await archive_artifact(artifact_uuid)
    if not archived:
        return {"error": f"Artifact not found: {artifact_id}"}
    return {"artifact_id": artifact_id, "archived": True}


@mcp_server.tool()
async def archive_artifact_slug(slug: str) -> dict[str, Any]:
    """Archive every active version of a TEXT artifact slug.

    Already-archived versions are skipped silently.

    Parameters:
        slug: The named_slug whose versions should all be archived.

    Returns:
        { success, slug, archived_count, message }
    """
    try:
        count = await archive_artifacts_by_slug(slug)
    except Exception:
        logger.exception("Failed to archive artifact slug", slug=slug)
        return {"success": False, "error": "Internal error archiving artifact slug."}
    return {
        "success": True,
        "slug": slug,
        "archived_count": count,
        "message": f"Archived {count} version(s) under slug '{slug}'.",
    }
