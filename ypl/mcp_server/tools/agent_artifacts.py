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
    get_artifact_by_id,
    get_artifact_by_slug,
    normalize_artifact_labels,
    read_artifact_content,
)
from ypl.backend.db import get_async_session_read_replica
from ypl.db.agent_harness import AgentArtifact, AgentArtifactType
from ypl.mcp_common.auth_context import current_request_context
from ypl.mcp_common.shared_tool import shared_tool
from ypl.mcp_server.tools import _arti_backend
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


def _caller_agent_name() -> str | None:
    """Return the calling agent's name from the typed RequestContext, or ``None``."""
    ctx = current_request_context()
    return ctx.ahs_agent_name if ctx is not None else None


def _caller_session_id() -> uuid.UUID | None:
    """Return the calling AHS session UUID, or ``None``."""
    ctx = current_request_context()
    if ctx is None:
        return None
    return _parse_session_id(ctx.ahs_session_id)


def _caller_user_id() -> str | None:
    """Return the calling ``user_id``, or ``None`` if not stamped."""
    ctx = current_request_context()
    return ctx.requesting_user_id if ctx is not None else None


async def _resolve_caller_context() -> tuple[uuid.UUID | None, str | None, uuid.UUID | None]:
    """Return (session_id, requesting_user_id, creator_agent_id) from the typed RequestContext."""
    ctx = current_request_context()
    if ctx is None:
        return None, None, None
    session_id = _parse_session_id(ctx.ahs_session_id)
    agent_id = await _resolve_agent_id(ctx.ahs_agent_name) if ctx.ahs_agent_name else None
    return session_id, ctx.requesting_user_id, agent_id


# ---------------------------------------------------------------------------
# Unified add_artifact (TEXT with content, or CODE_REVIEW / OTHER as pointer)
# ---------------------------------------------------------------------------


@shared_tool()
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
    labels: list[str] | None = None,
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
            For TEXT artifacts whose ``content`` begins with a top-level
            markdown heading (``# Some Title``), use the heading text as the
            title *and drop the ``# Some Title`` line from ``content``* — the
            viewer already renders the title prominently, so leaving the H1
            in the body double-titles the page. If the first line isn't a
            heading, choose a short title that summarizes the document.
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
        labels: Optional searchable labels. Labels are normalized to lowercase
            and stored in artifact_metadata.labels.

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
    if parsed_type == AgentArtifactType.MEMORY:
        # MEMORY artifacts have a dedicated tool surface (save_memory) because
        # they require scope + subject and scope-authz checks that this tool
        # doesn't model. Fail loudly rather than silently creating an
        # unreachable row.
        return {
            "success": False,
            "error": "Use save_memory for MEMORY artifacts — it threads scope/subject and authz automatically.",
        }

    parsed_task_id: uuid.UUID | None = None
    if agent_task_id:
        try:
            parsed_task_id = uuid.UUID(agent_task_id)
        except ValueError:
            return {"success": False, "error": f"Invalid agent_task_id '{agent_task_id}': not a valid UUID"}

    session_id, user_id, agent_id = await _resolve_caller_context()
    try:
        normalized_labels = normalize_artifact_labels(labels)
    except ArtifactError as exc:
        return {"success": False, "error": str(exc)}
    if normalized_labels:
        artifact_metadata = {**(artifact_metadata or {}), "labels": normalized_labels}

    agent_name = _caller_agent_name()
    prov = _arti_backend.provenance_labels(session_id=session_id, agent_name=agent_name, task_id=parsed_task_id)

    if parsed_type == AgentArtifactType.TEXT:
        if content is None:
            return {"success": False, "error": "TEXT artifacts require a 'content' body."}
        if attachments:
            return {"success": False, "error": "attachments are not yet supported for AL arti artifacts."}
        try:
            return await _arti_backend.add_text(
                user_id=user_id,
                session_id=session_id,
                title=title,
                content=content,
                content_type=content_type,
                named_slug=named_slug,
                create_new_slug=create_new_slug,
                description=description,
                labels=normalized_labels,
                prov=prov,
            )
        except _arti_backend.ArtiToolError as exc:
            return {"success": False, "error": f"AL arti rejected the write: {exc}"}
        except _arti_backend.ArtiUnavailable as exc:
            return {"success": False, "error": f"AL arti unavailable: {exc}"}

    # CODE_REVIEW / OTHER: pointer flavor, recorded in AL arti as a tagged TEXT record.
    if url is None:
        return {"success": False, "error": f"{artifact_type} artifacts require a 'url'."}
    try:
        return await _arti_backend.add_pointer(
            user_id=user_id,
            session_id=session_id,
            artifact_type=artifact_type,
            title=title,
            url=url,
            description=description,
            labels=normalized_labels,
            prov=prov,
        )
    except _arti_backend.ArtiToolError as exc:
        return {"success": False, "error": f"AL arti rejected the write: {exc}"}
    except _arti_backend.ArtiUnavailable as exc:
        return {"success": False, "error": f"AL arti unavailable: {exc}"}


@shared_tool()
async def update_artifact(
    artifact_id: str,
    title: str | None = None,
    description: str | None = None,
    url: str | None = None,
    artifact_metadata: dict[str, Any] | None = None,
    labels: list[str] | None = None,
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
        labels: Optional complete replacement label list.
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

    if labels is not None:
        try:
            artifact_metadata = {**(artifact_metadata or {}), "labels": normalize_artifact_labels(labels)}
        except ArtifactError as exc:
            return {"success": False, "error": str(exc)}

    if all(v is None for v in [title, description, url, artifact_metadata]):
        return {
            "success": False,
            "error": "At least one field (title, description, url, artifact_metadata) must be provided",
        }

    # AL arti's update surface covers title + labels (metadata). description / url
    # / arbitrary metadata aren't updatable in place there; pass what maps.
    updated_labels = None
    if artifact_metadata and "labels" in artifact_metadata:
        updated_labels = artifact_metadata["labels"]
    try:
        return await _arti_backend.update_meta(
            user_id=_caller_user_id(),
            ident=str(parsed_id),
            title=title,
            labels=updated_labels,
        )
    except _arti_backend.ArtiToolError as exc:
        if _arti_backend.arti_client.not_found(exc):
            return {"success": False, "error": f"Artifact '{artifact_id}' not found in AL arti."}
        return {"success": False, "error": f"AL arti update failed: {exc}"}
    except _arti_backend.ArtiUnavailable as exc:
        return {"success": False, "error": f"AL arti unavailable: {exc}"}


@shared_tool()
async def update_artifact_content(
    slug: str,
    content: str,
    title: str | None = None,
    description: str | None = None,
    content_type: str = "text/markdown",
    attachments: str | None = None,
    labels: list[str] | None = None,
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
        labels: Optional searchable labels for the new version.

    Returns:
        { success, artifact_id, url, slug, version, content_type, message } on success.
        { success: false, error } on failure.
    """
    try:
        decoded_attachments = _decode_attachments_arg(attachments)
    except ArtifactError as exc:
        return {"success": False, "error": str(exc)}

    if decoded_attachments:
        return {"success": False, "error": "attachments are not yet supported for AL arti artifacts."}
    session_id, user_id, agent_id = await _resolve_caller_context()
    resolved_title = title if title is not None else slug
    try:
        normalized_labels = normalize_artifact_labels(labels)
    except ArtifactError as exc:
        return {"success": False, "error": str(exc)}
    prov = _arti_backend.provenance_labels(
        session_id=session_id, agent_name=_caller_agent_name(), task_id=None
    )
    try:
        return await _arti_backend.add_version(
            user_id=user_id,
            session_id=session_id,
            slug=slug,
            content=content,
            content_type=content_type,
            title=resolved_title,
            description=description,
            labels=normalized_labels,
            prov=prov,
        )
    except _arti_backend.ArtiToolError as exc:
        return {"success": False, "error": f"AL arti rejected the write: {exc}"}
    except _arti_backend.ArtiUnavailable as exc:
        return {"success": False, "error": f"AL arti unavailable: {exc}"}


@shared_tool()
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
    limit = min(max(limit, 1), 100)
    agent_session_id = _caller_session_id()
    if agent_session_id is None:
        return {"success": True, "artifacts": [], "count": 0}

    if artifact_type and artifact_type not in _VALID_TYPES:
        return {
            "success": False,
            "error": f"Invalid artifact_type '{artifact_type}'. Must be one of: {_VALID_TYPES}",
        }
    try:
        return await _arti_backend.list_for_session(
            user_id=_caller_user_id(),
            session_id=agent_session_id,
            artifact_type=artifact_type,
            limit=limit,
        )
    except _arti_backend.ArtiUnavailable as exc:
        return {"success": False, "error": f"AL arti unavailable: {exc}"}
    except _arti_backend.ArtiToolError as exc:
        return {"success": False, "error": f"AL arti list failed: {exc}"}


@shared_tool()
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
        return await _arti_backend.list_versions(user_id=_caller_user_id(), slug=slug)
    except _arti_backend.ArtiUnavailable as exc:
        return {"success": False, "error": f"AL arti unavailable: {exc}"}
    except _arti_backend.ArtiToolError as exc:
        if _arti_backend.arti_client.not_found(exc):
            return {"success": True, "slug": slug, "versions": [], "count": 0}
        return {"success": False, "error": f"AL arti versions failed: {exc}"}


@shared_tool()
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
        return await _arti_backend.search(
            user_id=_caller_user_id(),
            query=query,
            artifact_type=artifact_type,
            limit=limit,
            offset=offset,
            include_archived=include_archived,
        )
    except _arti_backend.ArtiUnavailable as exc:
        return {"success": False, "error": f"AL arti unavailable: {exc}"}
    except _arti_backend.ArtiToolError as exc:
        return {"success": False, "error": f"AL arti search failed: {exc}"}


@shared_tool(
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
    """Fetch a TEXT artifact's content + metadata (AL arti; legacy fallback)."""
    # AL arti first.
    try:
        arti_res = await _arti_backend.read(user_id=_caller_user_id(), ident=id_or_slug, version=version)
        if arti_res is not None:
            return arti_res
    except _arti_backend.ArtiUnavailable:
        pass  # fall through to legacy read
    except _arti_backend.ArtiToolError as exc:
        if not _arti_backend.arti_client.not_found(exc):
            return {"error": f"AL arti read failed: {exc}"}

    # Legacy Yupp store fallback (pre-migration artifacts).
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
        "labels": (artifact.artifact_metadata or {}).get("labels", []),
    }


@shared_tool()
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
    # AL arti first.
    try:
        arti_res = await _arti_backend.url_of(user_id=_caller_user_id(), ident=id_or_slug)
        if arti_res is not None:
            return arti_res
    except _arti_backend.ArtiUnavailable:
        pass
    except _arti_backend.ArtiToolError as exc:
        if not _arti_backend.arti_client.not_found(exc):
            return {"success": False, "error": f"AL arti lookup failed: {exc}"}

    # Legacy Yupp store fallback.
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


@shared_tool(
    name="archive_artifact",
    description=(
        "Archive (soft-delete) a single artifact by UUID. The row stays in the "
        "DB with ``is_archived=true``; blobs are retained so the artifact can "
        "be un-archived later by a migration/admin if needed."
    ),
)
async def mcp_archive_artifact(artifact_id: str) -> dict[str, Any]:
    # AL arti first.
    try:
        ok = await _arti_backend.archive(user_id=_caller_user_id(), ident=artifact_id)
        if ok:
            return {"artifact_id": artifact_id, "archived": True}
    except _arti_backend.ArtiUnavailable:
        pass
    except _arti_backend.ArtiToolError as exc:
        if not _arti_backend.arti_client.not_found(exc):
            return {"error": f"AL arti archive failed: {exc}"}

    # Legacy Yupp store fallback.
    try:
        artifact_uuid = uuid.UUID(artifact_id)
    except ValueError:
        return {"error": f"Invalid artifact_id: {artifact_id!r}"}
    archived = await archive_artifact(artifact_uuid)
    if not archived:
        return {"error": f"Artifact not found: {artifact_id}"}
    return {"artifact_id": artifact_id, "archived": True}


@shared_tool()
async def archive_artifact_slug(slug: str) -> dict[str, Any]:
    """Archive every active version of a TEXT artifact slug.

    Already-archived versions are skipped silently.

    Parameters:
        slug: The named_slug whose versions should all be archived.

    Returns:
        { success, slug, archived_count, message }
    """
    # AL arti archives a whole slug when given the slug as ident.
    try:
        ok = await _arti_backend.archive(user_id=_caller_user_id(), ident=slug)
        if ok:
            return {
                "success": True,
                "slug": slug,
                "message": f"Archived slug '{slug}' in AL arti.",
            }
    except _arti_backend.ArtiUnavailable:
        pass
    except _arti_backend.ArtiToolError as exc:
        if not _arti_backend.arti_client.not_found(exc):
            return {"success": False, "error": f"AL arti archive failed: {exc}"}

    # Legacy Yupp store fallback.
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


# ---------------------------------------------------------------------------
# Memory MCP tools — scope-aware wrappers around MEMORY artifacts
# ---------------------------------------------------------------------------
#
# Moved to ``ypl/mcp_server/tools/memory_artifacts.py`` (registered there via
# ``@shared_tool()`` and imported by ``mcp_tools.py``).
