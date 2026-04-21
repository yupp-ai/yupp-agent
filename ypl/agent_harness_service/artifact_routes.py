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
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from ypl.agent_harness_service.artifact_store import (
    MAX_CONTENT_SIZE_BYTES,
    ArtifactError,
    Attachment,
    archive_artifact,
    create_artifact,
    get_artifact_by_id,
    get_artifact_by_slug,
    list_artifact_versions,
    list_artifacts,
    read_artifact_attachment,
    read_artifact_content,
    validate_named_slug,
)
from ypl.agent_harness_service.common.auth import verify_api_key
from ypl.db.agent_harness import AgentArtifact, AgentArtifactType
from ypl.structured_logger import get_logger

logger = get_logger()

artifact_router = APIRouter(prefix="/artifacts", dependencies=[Depends(verify_api_key)], tags=["artifacts"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class AttachmentPayload(BaseModel):
    """Attachment payload for creation requests."""

    filename: str = Field(..., description="Original filename")
    content_base64: str = Field(..., description="File contents, base64-encoded")
    content_type: str = Field(..., description="MIME type")


class CreateArtifactRequest(BaseModel):
    content: str = Field(..., description="Text content")
    content_type: str = Field("text/markdown", description="text/plain, text/markdown, or text/html")
    title: str = Field(..., description="Short title for the artifact")
    description: str | None = Field(None, description="Optional longer description")
    type: AgentArtifactType = Field(
        AgentArtifactType.YUPPASTE,
        description="Artifact type. Defaults to YUPPASTE (textual content).",
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
    url: str
    content_type: str | None
    named_slug: str | None
    version: int | None
    creator_user_id: str | None
    creator_agent_id: uuid.UUID | None
    agent_session_id: uuid.UUID | None
    agent_task_id: uuid.UUID | None
    created_at: datetime
    metadata: dict[str, Any] | None


class CreateArtifactResponse(ArtifactResponse):
    slug_url: str | None = None


class ArtifactListResponse(BaseModel):
    artifacts: list[ArtifactResponse]


class ArtifactVersionsResponse(BaseModel):
    named_slug: str
    versions: list[ArtifactResponse]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _artifact_to_response(artifact: AgentArtifact) -> ArtifactResponse:
    return ArtifactResponse(
        artifact_id=artifact.agent_artifact_id,
        type=artifact.artifact_type,
        title=artifact.title,
        description=artifact.description,
        url=artifact.url,
        content_type=artifact.content_type,
        named_slug=artifact.named_slug,
        version=artifact.version,
        creator_user_id=artifact.creator_user_id,
        creator_agent_id=artifact.creator_agent_id,
        agent_session_id=artifact.agent_session_id,
        agent_task_id=artifact.agent_task_id,
        created_at=artifact.created_at,
        metadata=artifact.artifact_metadata,
    )


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
async def create_artifact_route(request: CreateArtifactRequest) -> CreateArtifactResponse:
    """Create a new textual artifact and upload its content + attachments."""
    if len(request.content.encode("utf-8")) > MAX_CONTENT_SIZE_BYTES:
        raise HTTPException(status_code=413, detail=f"Content exceeds {MAX_CONTENT_SIZE_BYTES} bytes")
    try:
        artifact = await create_artifact(
            content=request.content.encode("utf-8"),
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
        )
    except ArtifactError as exc:
        raise _error_for(exc) from exc
    base = _artifact_to_response(artifact)
    slug_url = f"/ahs/artifacts/by-slug/{artifact.named_slug}" if artifact.named_slug else None
    return CreateArtifactResponse(**base.model_dump(), slug_url=slug_url)


_OPTIONAL_TYPE_QUERY = Query(None, alias="type")


@artifact_router.get("", response_model=ArtifactListResponse)
async def list_artifacts_route(
    artifact_type: AgentArtifactType | None = _OPTIONAL_TYPE_QUERY,
    creator_user_id: str | None = None,
    agent_session_id: uuid.UUID | None = None,
    agent_task_id: uuid.UUID | None = None,
    include_archived: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ArtifactListResponse:
    rows = await list_artifacts(
        artifact_type=artifact_type,
        creator_user_id=creator_user_id,
        agent_session_id=agent_session_id,
        agent_task_id=agent_task_id,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )
    return ArtifactListResponse(artifacts=[_artifact_to_response(a) for a in rows])


@artifact_router.get("/{artifact_id}", responses={200: {"content": {"*/*": {}}}})
async def read_artifact_route(artifact_id: uuid.UUID) -> Response:
    """Return the artifact's main content with its declared Content-Type."""
    artifact = await get_artifact_by_id(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"Artifact {artifact_id} not found")
    try:
        data, content_type = await read_artifact_content(artifact)
    except ArtifactError as exc:
        raise _error_for(exc) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"Artifact {artifact_id} metadata exists but content is missing"
        ) from exc
    return Response(content=data, media_type=content_type)


@artifact_router.get("/{artifact_id}/meta", response_model=ArtifactResponse)
async def read_artifact_meta_route(artifact_id: uuid.UUID) -> ArtifactResponse:
    artifact = await get_artifact_by_id(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"Artifact {artifact_id} not found")
    return _artifact_to_response(artifact)


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


_TYPE_QUERY = Query(AgentArtifactType.YUPPASTE, alias="type")


@artifact_router.get("/by-slug/{slug}", response_model=ArtifactResponse)
async def read_artifact_by_slug_route(
    slug: str,
    version: int | None = None,
    artifact_type: AgentArtifactType = _TYPE_QUERY,
) -> ArtifactResponse:
    """Resolve a slug to an artifact metadata record (latest non-archived by default)."""
    artifact = await get_artifact_by_slug(slug, version=version, artifact_type=artifact_type)
    if artifact is None:
        raise HTTPException(
            status_code=404,
            detail=f"No {artifact_type.value} artifact for slug {slug!r}"
            + (f" at version {version}" if version is not None else ""),
        )
    return _artifact_to_response(artifact)


@artifact_router.get("/by-slug/{slug}/versions", response_model=ArtifactVersionsResponse)
async def list_versions_route(
    slug: str,
    artifact_type: AgentArtifactType = _TYPE_QUERY,
) -> ArtifactVersionsResponse:
    rows = await list_artifact_versions(slug, artifact_type=artifact_type)
    return ArtifactVersionsResponse(
        named_slug=slug,
        versions=[_artifact_to_response(a) for a in rows],
    )


@artifact_router.delete("/{artifact_id}", status_code=204)
async def archive_artifact_route(artifact_id: uuid.UUID) -> Response:
    """Archive an artifact (sets ``is_archived=true`` in metadata; blobs retained)."""
    archived = await archive_artifact(artifact_id)
    if not archived:
        raise HTTPException(status_code=404, detail=f"Artifact {artifact_id} not found")
    return Response(status_code=204)
