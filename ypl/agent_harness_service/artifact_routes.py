"""REST routes for the textual artifact system.

Mounted on AHS at ``/ahs/artifacts`` — callers are agents (via the MCP
tool layer) and trusted internal services (via ``X-API-Key``). A future
``artifacts.agcouch.com`` viewer app will call these same routes with
its own OAuth layer in front.

Machine-readable only: content is returned as raw bytes with the
appropriate ``Content-Type`` header, metadata is JSON. HTML rendering
(markdown → HTML, attachments inline, etc.) lives in the viewer app,
not here.
"""

from __future__ import annotations
import re
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from ypl.agent_harness_service.artifact_store import (
    MAX_CONTENT_SIZE_BYTES,
    VALID_MEMORY_SCOPES,
    ArtifactError,
    Attachment,
    MemoryCallerContext,
    archive_artifact,
    archive_artifacts_by_slug,
    caller_can_write_memory,
    count_artifacts,
    create_artifact,
    get_artifact_by_id,
    get_artifact_by_slug,
    list_artifact_versions,
    list_artifacts,
    list_distinct_creators,
    read_artifact_attachment,
    read_artifact_content,
    resolve_attribution,
    search_artifacts,
    validate_memory_scope_shape,
    validate_named_slug,
)
from ypl.agent_harness_service.common.auth import verify_api_key
from ypl.db.agent_harness import AgentArtifact, AgentArtifactType
from ypl.structured_logger import get_logger

logger = get_logger()

# Extensions used when building the download filename from an artifact's
# Content-Type. Kept narrow on purpose — the common textual types we actually
# serve. Anything unrecognized falls back to the artifact UUID with no suffix.
_CONTENT_TYPE_EXT: dict[str, str] = {
    "text/markdown": ".md",
    "text/html": ".html",
    "text/plain": ".txt",
    "application/json": ".json",
    "text/csv": ".csv",
}

# Characters that are unsafe in filenames on common filesystems, plus control
# chars. Replaced with a single space when sanitizing.
_FILENAME_UNSAFE_RE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


def _download_filename(title: str | None, content_type: str | None) -> tuple[str, str]:
    """Build ``(ascii_fallback, utf8_percent_encoded)`` filenames for a download.

    The ascii fallback replaces non-ASCII characters with ``_`` so very old
    browsers still get something sensible; the UTF-8 variant preserves the
    original title via RFC 5987 percent-encoding. Both include an extension
    derived from ``content_type`` when we recognize it.
    """
    base = (title or "").strip()
    # Collapse whitespace and strip unsafe chars. Keep the title readable — no
    # aggressive slug conversion; a user's download should match what they saw.
    base = _FILENAME_UNSAFE_RE.sub(" ", base)
    base = re.sub(r"\s+", " ", base).strip(" .")
    if not base:
        base = "artifact"
    # Keep the filename manageable for downstream tooling.
    base = base[:180]

    ext = ""
    if content_type:
        # Strip parameters like ``; charset=utf-8`` before matching.
        primary = content_type.split(";", 1)[0].strip().lower()
        ext = _CONTENT_TYPE_EXT.get(primary, "")

    utf8_name = f"{base}{ext}"
    ascii_name = utf8_name.encode("ascii", "replace").decode("ascii").replace("?", "_")
    return ascii_name, quote(utf8_name, safe="")


artifact_router = APIRouter(prefix="/artifacts", dependencies=[Depends(verify_api_key)], tags=["artifacts"])


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


def _caller_can_read_memory(caller: MemoryCallerContext, scope: str | None, subject: str | None) -> bool:
    """Return True if the caller may read MEMORY rows in (scope, subject).

    Admin callers (no identity) can read anything. Identity-bearing callers
    are bound by :func:`memory_read_clause` — here we pre-check the scope/
    subject filter so an out-of-scope request returns 403 instead of an
    empty result set.

    ``subject=None`` is permitted for ``scope=user`` / ``scope=agent``
    because the route layer defaults the subject to the caller's own
    user_id / agent_name. The authz check runs again on the resolved
    subject inside ``_resolve_memory_slug_scope``.
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


def _validate_scope_query(
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
    if not _caller_can_read_memory(caller, scope, subject):
        raise HTTPException(
            status_code=403,
            detail=f"Caller not permitted to read memory_scope={scope!r}, subject={subject!r}",
        )


_MEMORY_SCOPE_QUERY = Query(
    None,
    alias="scope",
    description="Memory scope filter (MEMORY artifacts only): 'user', 'agent', or 'topic'.",
)
_MEMORY_SUBJECT_QUERY = Query(
    None,
    alias="subject",
    description="Memory subject filter (MEMORY artifacts only): user_id for scope=user, agent name for scope=agent.",
)

# Module-level Depends singleton so ruff B008 (no function calls in argument
# defaults) is happy while we still get FastAPI's per-request injection.
_CALLER_CTX_DEP = Depends(caller_context_from_request)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class AttachmentPayload(BaseModel):
    """Attachment payload for creation requests."""

    filename: str = Field(..., description="Original filename")
    content_base64: str = Field(..., description="File contents, base64-encoded")
    content_type: str = Field(..., description="MIME type")


class CreateArtifactRequest(BaseModel):
    content: str | None = Field(
        None,
        description="Textual body for blob-backed artifacts (TEXT). Ignored for MEMORY — use inline_content instead.",
    )
    inline_content: str | None = Field(
        None,
        description="Inline body for MEMORY artifacts. Stored directly on the row, not uploaded to the blob store.",
    )
    content_type: str = Field("text/markdown", description="text/plain, text/markdown, or text/html")
    title: str = Field(..., description="Short title for the artifact")
    description: str | None = Field(None, description="Optional longer description")
    type: AgentArtifactType = Field(
        AgentArtifactType.TEXT,
        description="Artifact type. Defaults to TEXT (textual content).",
    )
    memory_scope: str | None = Field(
        None,
        description="MEMORY artifacts only: 'user', 'agent', or 'topic'. Required when type=MEMORY.",
    )
    memory_scope_subject: str | None = Field(
        None,
        description=(
            "MEMORY artifacts only: users.user_id (scope=user), agents.name (scope=agent), or NULL (scope=topic)."
        ),
    )
    named_slug: str | None = Field(None, description="Stable URL-safe slug")
    create_new_slug: bool = Field(False, description="True → allocate a fresh slug. False → append next version.")
    creator_user_id: str | None = Field(None, description="User ID of the creator")
    agent_session_id: uuid.UUID | None = Field(None, description="Session to attribute to")
    agent_task_id: uuid.UUID | None = Field(None, description="Task to attribute to")
    attachments: list[AttachmentPayload] | None = Field(None, description="Sibling files")
    metadata: dict[str, Any] | None = Field(None, description="Extra metadata stored alongside")

    @field_validator("named_slug")
    @classmethod
    def _validate_slug(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                validate_named_slug(v)
            except ArtifactError as exc:
                # Re-raise as ValueError so pydantic maps it to 422.
                raise ValueError(str(exc)) from exc
        return v


class ArtifactResponse(BaseModel):
    """Metadata-only JSON view of an artifact."""

    artifact_id: uuid.UUID
    type: AgentArtifactType
    title: str
    description: str | None
    # MEMORY artifacts have no url (inline_content lives on the row). Other
    # types always populate url.
    url: str | None
    content_type: str | None
    named_slug: str | None
    version: int | None
    # MEMORY fields — always present in the response (null for non-MEMORY rows)
    # so clients have a uniform shape.
    memory_scope: str | None = None
    memory_scope_subject: str | None = None
    creator_user_id: str | None
    creator_user_name: str | None = None
    creator_agent_id: uuid.UUID | None
    creator_agent_name: str | None = None
    agent_session_id: uuid.UUID | None
    agent_task_id: uuid.UUID | None
    created_at: datetime
    metadata: dict[str, Any] | None


class CreateArtifactResponse(ArtifactResponse):
    slug_url: str | None = None


class ArtifactListResponse(BaseModel):
    artifacts: list[ArtifactResponse]
    # Total count of rows matching the same filters (independent of limit/offset).
    # Optional so existing callers (e.g. legacy clients) keep working — filled in
    # by ``list_artifacts_route`` when the caller passes ``include_total=true``.
    total: int | None = None


class CreatorOption(BaseModel):
    """One entry in the creators-list response used by filter dropdowns."""

    id: str
    name: str | None = None


class ArtifactCreatorsResponse(BaseModel):
    """Distinct user/agent creators that have at least one artifact."""

    users: list[CreatorOption]
    agents: list[CreatorOption]


class ArtifactVersionsResponse(BaseModel):
    named_slug: str
    versions: list[ArtifactResponse]


class ArchiveBySlugResponse(BaseModel):
    named_slug: str
    archived_count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _artifact_to_response(
    artifact: AgentArtifact,
    *,
    user_names: dict[str, str] | None = None,
    agent_names: dict[uuid.UUID, str] | None = None,
) -> ArtifactResponse:
    user_name = None
    if artifact.creator_user_id and user_names:
        user_name = user_names.get(artifact.creator_user_id)
    agent_name = None
    if artifact.creator_agent_id and agent_names:
        agent_name = agent_names.get(artifact.creator_agent_id)
    return ArtifactResponse(
        artifact_id=artifact.agent_artifact_id,
        type=artifact.artifact_type,
        title=artifact.title,
        description=artifact.description,
        url=artifact.url,
        content_type=artifact.content_type,
        named_slug=artifact.named_slug,
        version=artifact.version,
        memory_scope=artifact.memory_scope,
        memory_scope_subject=artifact.memory_scope_subject,
        creator_user_id=artifact.creator_user_id,
        creator_user_name=user_name,
        creator_agent_id=artifact.creator_agent_id,
        creator_agent_name=agent_name,
        agent_session_id=artifact.agent_session_id,
        agent_task_id=artifact.agent_task_id,
        created_at=artifact.created_at,
        metadata=artifact.artifact_metadata,
    )


async def _artifacts_to_response_list(artifacts: list[AgentArtifact]) -> list[ArtifactResponse]:
    """Resolve attribution names in bulk and build the response list."""
    user_names, agent_names = await resolve_attribution(artifacts)
    return [_artifact_to_response(a, user_names=user_names, agent_names=agent_names) for a in artifacts]


def _decode_attachments(payload: list[AttachmentPayload] | None) -> list[Attachment]:
    import base64

    result: list[Attachment] = []
    for item in payload or []:
        try:
            data = base64.b64decode(item.content_base64)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid base64 for attachment {item.filename!r}") from exc
        result.append(Attachment(filename=item.filename, data=data, content_type=item.content_type))
    return result


def _error_for(exc: ArtifactError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@artifact_router.post("", response_model=CreateArtifactResponse, status_code=201)
async def create_artifact_route(
    request: CreateArtifactRequest,
    caller: MemoryCallerContext = _CALLER_CTX_DEP,
) -> CreateArtifactResponse:
    """Create a new artifact.

    * Non-MEMORY types: ``content`` is uploaded to the blob store and a
      canonical viewer URL is generated.
    * MEMORY type: ``inline_content`` is written directly to the row, the
      blob store is skipped, and ``memory_scope`` (+ ``memory_scope_subject``
      for user/agent scopes) must be set. Writes are refused with 403 when
      the caller's identity doesn't match ``memory_scope_subject`` for
      user/agent scopes.
    """
    is_memory = request.type == AgentArtifactType.MEMORY

    # Shape validation for MEMORY requests.
    if is_memory:
        if request.inline_content is None:
            raise HTTPException(status_code=400, detail="MEMORY artifacts require 'inline_content'")
        if request.memory_scope is None:
            raise HTTPException(status_code=400, detail="MEMORY artifacts require 'memory_scope'")
        try:
            validate_memory_scope_shape(request.memory_scope, request.memory_scope_subject)
        except ArtifactError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not caller_can_write_memory(caller, request.memory_scope, request.memory_scope_subject):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Caller not permitted to write memory_scope={request.memory_scope!r}, "
                    f"subject={request.memory_scope_subject!r}"
                ),
            )
        inline_size = len(request.inline_content.encode("utf-8"))
        if inline_size > MAX_CONTENT_SIZE_BYTES:
            raise HTTPException(status_code=413, detail=f"inline_content exceeds {MAX_CONTENT_SIZE_BYTES} bytes")
    else:
        if request.content is None:
            raise HTTPException(status_code=400, detail=f"{request.type.value} artifacts require 'content'")
        if len(request.content.encode("utf-8")) > MAX_CONTENT_SIZE_BYTES:
            raise HTTPException(status_code=413, detail=f"Content exceeds {MAX_CONTENT_SIZE_BYTES} bytes")
        if request.inline_content is not None:
            raise HTTPException(
                status_code=400,
                detail=f"inline_content is only valid for MEMORY (got type={request.type.value})",
            )
        if request.memory_scope is not None or request.memory_scope_subject is not None:
            raise HTTPException(
                status_code=400,
                detail=f"memory_scope/subject only apply to MEMORY (got type={request.type.value})",
            )

    try:
        artifact = await create_artifact(
            content=request.content.encode("utf-8") if (request.content is not None and not is_memory) else None,
            inline_content=request.inline_content if is_memory else None,
            content_type=request.content_type,
            title=request.title,
            description=request.description,
            creator_user_id=request.creator_user_id,
            agent_session_id=request.agent_session_id,
            agent_task_id=request.agent_task_id,
            named_slug=request.named_slug,
            create_new_slug=request.create_new_slug,
            attachments=_decode_attachments(request.attachments),
            extra_metadata=request.metadata,
            artifact_type=request.type,
            memory_scope=request.memory_scope,
            memory_scope_subject=request.memory_scope_subject,
        )
    except ArtifactError as exc:
        raise _error_for(exc) from exc
    user_names, agent_names = await resolve_attribution([artifact])
    base = _artifact_to_response(artifact, user_names=user_names, agent_names=agent_names)
    slug_url = f"/ahs/artifacts/by-slug/{artifact.named_slug}" if artifact.named_slug else None
    return CreateArtifactResponse(**base.model_dump(), slug_url=slug_url)


_OPTIONAL_TYPE_QUERY = Query(None, alias="type")
_CREATED_AFTER_QUERY = Query(
    None,
    description="Lower bound on created_at (ISO 8601, inclusive). Naive timestamps are interpreted as UTC.",
)
_CREATED_BEFORE_QUERY = Query(
    None,
    description="Upper bound on created_at (ISO 8601, exclusive). Naive timestamps are interpreted as UTC.",
)
_INCLUDE_TOTAL_QUERY = Query(
    False,
    description="When true, also return the total count of matching rows (used by paginated UIs).",
)


@artifact_router.get("", response_model=ArtifactListResponse)
async def list_artifacts_route(
    artifact_type: AgentArtifactType | None = _OPTIONAL_TYPE_QUERY,
    creator_user_id: str | None = None,
    creator_agent_id: uuid.UUID | None = None,
    agent_session_id: uuid.UUID | None = None,
    agent_task_id: uuid.UUID | None = None,
    created_after: datetime | None = _CREATED_AFTER_QUERY,
    created_before: datetime | None = _CREATED_BEFORE_QUERY,
    include_archived: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_total: bool = _INCLUDE_TOTAL_QUERY,
    scope: str | None = _MEMORY_SCOPE_QUERY,
    subject: str | None = _MEMORY_SUBJECT_QUERY,
    caller: MemoryCallerContext = _CALLER_CTX_DEP,
) -> ArtifactListResponse:
    """List artifacts. MEMORY rows are filtered by the caller's visibility.

    When ``type=MEMORY`` (or left unset so MEMORY rows could appear), the
    caller's ``X-User-ID`` / ``X-AHS-Agent-Name`` identity restricts the
    visible MEMORY rows to ``topic`` + own user + own agent. ``scope=`` +
    ``subject=`` query params narrow further; cross-scope reads return 403.
    """
    _validate_scope_query(scope=scope, subject=subject, caller=caller)
    # For identity-bearing callers, thread the caller through so the store
    # can apply the MEMORY read filter. Admin callers (no identity) skip the
    # filter and see everything.
    memory_caller = caller if caller.has_identity else None
    # If scope=user/agent was specified without a subject and the caller has
    # an identity, default to the caller's own subject. This is the common
    # "just list my memories" path.
    effective_subject = subject
    if scope in ("user", "agent") and subject is None:
        effective_subject = caller.user_id if scope == "user" else caller.agent_name
    rows = await list_artifacts(
        artifact_type=artifact_type,
        creator_user_id=creator_user_id,
        creator_agent_id=creator_agent_id,
        agent_session_id=agent_session_id,
        agent_task_id=agent_task_id,
        created_after=created_after,
        created_before=created_before,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
        memory_caller=memory_caller,
        memory_scope=scope,
        memory_scope_subject=effective_subject,
    )
    total: int | None = None
    if include_total:
        total = await count_artifacts(
            artifact_type=artifact_type,
            creator_user_id=creator_user_id,
            creator_agent_id=creator_agent_id,
            agent_session_id=agent_session_id,
            agent_task_id=agent_task_id,
            created_after=created_after,
            created_before=created_before,
            include_archived=include_archived,
            memory_caller=memory_caller,
            memory_scope=scope,
            memory_scope_subject=effective_subject,
        )
    return ArtifactListResponse(artifacts=await _artifacts_to_response_list(rows), total=total)


@artifact_router.get("/creators", response_model=ArtifactCreatorsResponse)
async def list_creators_route(
    include_archived: bool = False,
) -> ArtifactCreatorsResponse:
    """List distinct user/agent creators across all artifacts.

    Powers the creator dropdowns in the artifact viewer's filter row so the
    options stay scoped to creators that actually have at least one artifact.
    """
    users, agents = await list_distinct_creators(include_archived=include_archived)
    return ArtifactCreatorsResponse(
        users=[CreatorOption(id=uid, name=name) for uid, name in users],
        agents=[CreatorOption(id=str(aid), name=name) for aid, name in agents],
    )


# IMPORTANT: ``/search`` (and ``/creators`` above) must be declared before the
# ``/{artifact_id}`` routes below — FastAPI matches path templates in
# declaration order, and these literal segments would otherwise be interpreted
# as (malformed) UUIDs.
@artifact_router.get("/search", response_model=ArtifactListResponse)
async def search_artifacts_route(
    q: str = Query(..., min_length=1, description="Substring to match (case-insensitive)"),
    artifact_type: AgentArtifactType | None = _OPTIONAL_TYPE_QUERY,
    include_archived: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    scope: str | None = _MEMORY_SCOPE_QUERY,
    subject: str | None = _MEMORY_SUBJECT_QUERY,
    caller: MemoryCallerContext = _CALLER_CTX_DEP,
) -> ArtifactListResponse:
    """Substring search over title, description, slug, inline_content, and attachment filenames.

    MEMORY rows are filtered by the caller's visibility (see
    :func:`list_artifacts_route`).
    """
    _validate_scope_query(scope=scope, subject=subject, caller=caller)
    memory_caller = caller if caller.has_identity else None
    effective_subject = subject
    if scope in ("user", "agent") and subject is None:
        effective_subject = caller.user_id if scope == "user" else caller.agent_name
    rows = await search_artifacts(
        q,
        artifact_type=artifact_type,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
        memory_caller=memory_caller,
        memory_scope=scope,
        memory_scope_subject=effective_subject,
    )
    return ArtifactListResponse(artifacts=await _artifacts_to_response_list(rows))


def _enforce_memory_read_permission(artifact: AgentArtifact, caller: MemoryCallerContext) -> None:
    """Refuse reads of a MEMORY row that the caller has no right to see.

    Admin callers (no identity) bypass this check. For identity-bearing
    callers, the row is readable iff it's in the caller's visibility set
    (``topic`` ∪ user/self ∪ agent/self).
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


@artifact_router.get("/{artifact_id}", responses={200: {"content": {"*/*": {}}}})
async def read_artifact_route(
    artifact_id: uuid.UUID,
    caller: MemoryCallerContext = _CALLER_CTX_DEP,
) -> Response:
    """Return the artifact's main content with its declared Content-Type.

    Sets ``Content-Disposition`` so that when this URL is opened via the
    viewer's "Raw content" link, the browser saves the file with the
    artifact's title as the filename (plus a content-type-appropriate
    extension). Programmatic API consumers read the response body directly
    and are unaffected by the header.

    MEMORY rows the caller has no right to see return 404 (not 403) — we
    don't leak existence across user/agent scopes.
    """
    artifact = await get_artifact_by_id(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"Artifact {artifact_id} not found")
    _enforce_memory_read_permission(artifact, caller)
    try:
        data, content_type = await read_artifact_content(artifact)
    except ArtifactError as exc:
        raise _error_for(exc) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"Artifact {artifact_id} metadata exists but content is missing"
        ) from exc
    ascii_name, utf8_name = _download_filename(artifact.title, content_type)
    headers = {"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"}
    return Response(content=data, media_type=content_type, headers=headers)


@artifact_router.get("/{artifact_id}/meta", response_model=ArtifactResponse)
async def read_artifact_meta_route(
    artifact_id: uuid.UUID,
    caller: MemoryCallerContext = _CALLER_CTX_DEP,
) -> ArtifactResponse:
    artifact = await get_artifact_by_id(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"Artifact {artifact_id} not found")
    _enforce_memory_read_permission(artifact, caller)
    user_names, agent_names = await resolve_attribution([artifact])
    return _artifact_to_response(artifact, user_names=user_names, agent_names=agent_names)


@artifact_router.get("/{artifact_id}/attachments/{filename}")
async def read_attachment_route(artifact_id: uuid.UUID, filename: str) -> Response:
    artifact = await get_artifact_by_id(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"Artifact {artifact_id} not found")
    try:
        data, content_type = await read_artifact_attachment(artifact, filename)
    except ArtifactError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Attachment {filename!r} blob missing") from exc
    return Response(content=data, media_type=content_type)


_TYPE_QUERY = Query(AgentArtifactType.TEXT, alias="type")


def _resolve_memory_slug_scope(
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
    if artifact_type != AgentArtifactType.MEMORY:
        return None, None
    if scope is None:
        raise HTTPException(
            status_code=400,
            detail="MEMORY slug routes require a 'scope' query parameter (user|agent|topic).",
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
        if not _caller_can_read_memory(caller, scope, effective_subject):
            raise HTTPException(
                status_code=403,
                detail=f"Caller not permitted to read memory_scope={scope!r}, subject={effective_subject!r}",
            )
    return scope, effective_subject


@artifact_router.get("/by-slug/{slug}", response_model=ArtifactResponse)
async def read_artifact_by_slug_route(
    slug: str,
    version: int | None = None,
    artifact_type: AgentArtifactType = _TYPE_QUERY,
    scope: str | None = _MEMORY_SCOPE_QUERY,
    subject: str | None = _MEMORY_SUBJECT_QUERY,
    caller: MemoryCallerContext = _CALLER_CTX_DEP,
) -> ArtifactResponse:
    """Resolve a slug to an artifact metadata record (latest non-archived by default).

    For ``type=MEMORY`` the ``scope`` query param is required; ``subject``
    defaults to the caller's own user_id/agent_name when omitted.
    """
    eff_scope, eff_subject = _resolve_memory_slug_scope(
        artifact_type=artifact_type, scope=scope, subject=subject, caller=caller
    )
    try:
        artifact = await get_artifact_by_slug(
            slug,
            version=version,
            artifact_type=artifact_type,
            memory_scope=eff_scope,
            memory_scope_subject=eff_subject,
        )
    except ArtifactError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if artifact is None:
        raise HTTPException(
            status_code=404,
            detail=f"No {artifact_type.value} artifact for slug {slug!r}"
            + (f" at version {version}" if version is not None else ""),
        )
    user_names, agent_names = await resolve_attribution([artifact])
    return _artifact_to_response(artifact, user_names=user_names, agent_names=agent_names)


@artifact_router.get("/by-slug/{slug}/versions", response_model=ArtifactVersionsResponse)
async def list_versions_route(
    slug: str,
    artifact_type: AgentArtifactType = _TYPE_QUERY,
    scope: str | None = _MEMORY_SCOPE_QUERY,
    subject: str | None = _MEMORY_SUBJECT_QUERY,
    caller: MemoryCallerContext = _CALLER_CTX_DEP,
) -> ArtifactVersionsResponse:
    eff_scope, eff_subject = _resolve_memory_slug_scope(
        artifact_type=artifact_type, scope=scope, subject=subject, caller=caller
    )
    try:
        rows = await list_artifact_versions(
            slug,
            artifact_type=artifact_type,
            memory_scope=eff_scope,
            memory_scope_subject=eff_subject,
        )
    except ArtifactError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ArtifactVersionsResponse(
        named_slug=slug,
        versions=await _artifacts_to_response_list(rows),
    )


@artifact_router.delete("/{artifact_id}", status_code=204)
async def archive_artifact_route(
    artifact_id: uuid.UUID,
    caller: MemoryCallerContext = _CALLER_CTX_DEP,
) -> Response:
    """Archive an artifact (sets ``is_archived=true`` in metadata; blobs retained)."""
    # MEMORY rows: enforce write-level scope authz before archiving.
    artifact = await get_artifact_by_id(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"Artifact {artifact_id} not found")
    if artifact.artifact_type == AgentArtifactType.MEMORY:
        if not caller_can_write_memory(caller, artifact.memory_scope or "", artifact.memory_scope_subject):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Caller not permitted to archive memory_scope={artifact.memory_scope!r}, "
                    f"subject={artifact.memory_scope_subject!r}"
                ),
            )
    archived = await archive_artifact(artifact_id)
    if not archived:
        raise HTTPException(status_code=404, detail=f"Artifact {artifact_id} not found")
    return Response(status_code=204)


@artifact_router.delete("/by-slug/{slug}", response_model=ArchiveBySlugResponse)
async def archive_by_slug_route(
    slug: str,
    artifact_type: AgentArtifactType = _TYPE_QUERY,
    scope: str | None = _MEMORY_SCOPE_QUERY,
    subject: str | None = _MEMORY_SUBJECT_QUERY,
    caller: MemoryCallerContext = _CALLER_CTX_DEP,
) -> ArchiveBySlugResponse:
    """Archive every non-archived version of an artifact slug.

    Returns 404 only if no versions exist for the slug at all; an
    already-fully-archived slug returns 200 with ``archived_count=0``. For
    MEMORY slugs the caller must be authorized to write to (scope, subject).
    """
    eff_scope, eff_subject = _resolve_memory_slug_scope(
        artifact_type=artifact_type, scope=scope, subject=subject, caller=caller, require_write=True
    )
    try:
        existing = await list_artifact_versions(
            slug,
            artifact_type=artifact_type,
            memory_scope=eff_scope,
            memory_scope_subject=eff_subject,
        )
    except ArtifactError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not existing:
        raise HTTPException(status_code=404, detail=f"No {artifact_type.value} artifact for slug {slug!r}")
    count = await archive_artifacts_by_slug(
        slug,
        artifact_type=artifact_type,
        memory_scope=eff_scope,
        memory_scope_subject=eff_subject,
    )
    return ArchiveBySlugResponse(named_slug=slug, archived_count=count)
