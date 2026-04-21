"""Service layer for textual artifacts.

Writes artifact content to the configured ``BlobStore`` and metadata to
the ``agent_artifacts`` Postgres table. Loads go through the same two
stores. The ``YUPPASTE`` enum value is kept for schema stability, but
semantically these are just "textual artifacts" — content is stored
inline in the blob store, siblings are attachments, and metadata
(title, slug, version, creator, session/task linkage) lives in
``agent_artifacts``.

Path convention (passed to the BlobStore):

    {uuid[:2]}/{uuid}/{uuid}.{ext}   # main content
    {uuid[:2]}/{uuid}/{filename}      # attachments

Extensions are derived from ``content_type``. Only three content types
are supported for the main file today: text/plain, text/markdown,
text/html. Attachments can be any MIME type.
"""

from __future__ import annotations
import os
import re
import uuid
from typing import Any

from sqlalchemy import func, or_, text
from sqlmodel import col, select

from ypl.backend.db import get_async_session, get_async_session_read_replica, retry_db
from ypl.backend.utils.blob_store import BlobStore, get_blob_store
from ypl.db.agent_harness import AgentArtifact, AgentArtifactType
from ypl.structured_logger import get_logger

logger = get_logger()

# Supported textual content types + their file extensions.
CONTENT_TYPE_EXTENSIONS: dict[str, str] = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/html": ".html",
}

# URL-safe slug pattern: 1-63 chars, starts alphanumeric, then alphanumeric / - / _.
_NAMED_SLUG_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")

# Hard limit on content uploaded inline through the creation tools.
MAX_CONTENT_SIZE_BYTES = 10 * 1024 * 1024  # 10 MiB

# Attachment rules.
MAX_ATTACHMENT_SIZE_BYTES = 10 * 1024 * 1024  # 10 MiB per file
MAX_ATTACHMENTS_PER_ARTIFACT = 20
_INVALID_FILENAME_CHARS = frozenset({"/", "\\", "\x00"})
_INVALID_FILENAMES = frozenset({".", "..", ""})


class ArtifactError(Exception):
    """Raised for validation / lifecycle errors in the artifact store."""


def validate_named_slug(slug: str) -> None:
    """Raise :class:`ArtifactError` if ``slug`` is not URL-safe."""
    if not _NAMED_SLUG_PATTERN.match(slug):
        raise ArtifactError(
            "named_slug must be 1-63 chars, start with an alphanumeric, "
            "and contain only alphanumeric characters, hyphens, or underscores."
        )


def _extension_for(content_type: str) -> str:
    try:
        return CONTENT_TYPE_EXTENSIONS[content_type]
    except KeyError as exc:
        raise ArtifactError(
            f"Unsupported content_type: {content_type!r}. Allowed: {sorted(CONTENT_TYPE_EXTENSIONS)}"
        ) from exc


def content_path_for(artifact_id: uuid.UUID | str, content_type: str) -> str:
    """Logical blob path for an artifact's main content file."""
    aid = str(artifact_id)
    ext = _extension_for(content_type)
    return f"{aid[:2]}/{aid}/{aid}{ext}"


def attachment_path_for(artifact_id: uuid.UUID | str, filename: str) -> str:
    """Logical blob path for an attachment under an artifact."""
    aid = str(artifact_id)
    return f"{aid[:2]}/{aid}/{_sanitize_attachment_filename(filename)}"


def _sanitize_attachment_filename(filename: str) -> str:
    """Strip to basename and reject path-traversal-shaped filenames."""
    filename = os.path.basename(filename)
    if filename in _INVALID_FILENAMES or any(c in filename for c in _INVALID_FILENAME_CHARS):
        raise ArtifactError(f"Invalid attachment filename: {filename!r}")
    return filename


class Attachment:
    """In-memory attachment used at creation time."""

    __slots__ = ("filename", "data", "content_type")

    def __init__(self, *, filename: str, data: bytes, content_type: str) -> None:
        self.filename = _sanitize_attachment_filename(filename)
        self.data = data
        self.content_type = content_type
        if len(data) > MAX_ATTACHMENT_SIZE_BYTES:
            raise ArtifactError(f"Attachment {self.filename!r} exceeds {MAX_ATTACHMENT_SIZE_BYTES} bytes")


# ---------------------------------------------------------------------------
# Slug / version helpers (Postgres-backed)
# ---------------------------------------------------------------------------


async def _max_version_for_slug(slug: str, artifact_type: AgentArtifactType) -> int | None:
    """Return the highest existing version for ``slug`` (including archived)."""
    async with get_async_session_read_replica() as session:
        stmt = select(func.max(AgentArtifact.version)).where(
            AgentArtifact.named_slug == slug,
            AgentArtifact.artifact_type == artifact_type,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def _slug_has_active_version(slug: str, artifact_type: AgentArtifactType) -> bool:
    """True if any non-archived version of ``slug`` exists for ``artifact_type``."""
    async with get_async_session_read_replica() as session:
        stmt = (
            select(AgentArtifact.agent_artifact_id)
            .where(
                AgentArtifact.named_slug == slug,
                AgentArtifact.artifact_type == artifact_type,
                col(AgentArtifact.deleted_at).is_(None),
                # is_archived stored in artifact_metadata JSONB.
                text("(agent_artifacts.artifact_metadata->>'is_archived') IS DISTINCT FROM 'true'"),
            )
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.first() is not None


async def _resolve_next_version(
    *,
    slug: str | None,
    create_new_slug: bool,
    artifact_type: AgentArtifactType,
) -> int | None:
    """Return the ``version`` to assign to a new artifact, or ``None`` if slug is unset."""
    if slug is None:
        if create_new_slug:
            raise ArtifactError("create_new_slug=True requires named_slug")
        return None

    validate_named_slug(slug)
    existing_max = await _max_version_for_slug(slug, artifact_type)
    has_active = await _slug_has_active_version(slug, artifact_type)

    if create_new_slug:
        if has_active:
            raise ArtifactError(
                f"named_slug {slug!r} already has active versions; set create_new_slug=False to append a new version."
            )
        # New slug, version 1 (or max+1 if all prior versions were archived).
        return (existing_max or 0) + 1

    # Append to existing slug.
    if existing_max is None:
        raise ArtifactError(f"named_slug {slug!r} does not exist yet; set create_new_slug=True to create it.")
    return existing_max + 1


# ---------------------------------------------------------------------------
# Create / read / delete
# ---------------------------------------------------------------------------


@retry_db
async def _insert_artifact_row(artifact: AgentArtifact) -> AgentArtifact:
    async with get_async_session() as session:
        session.add(artifact)
        await session.commit()
        await session.refresh(artifact)
        return artifact


async def create_artifact(
    *,
    content: bytes,
    content_type: str,
    title: str,
    description: str | None = None,
    creator_user_id: str | None = None,
    creator_agent_id: uuid.UUID | None = None,
    agent_session_id: uuid.UUID | None = None,
    agent_task_id: uuid.UUID | None = None,
    named_slug: str | None = None,
    create_new_slug: bool = False,
    attachments: list[Attachment] | None = None,
    extra_metadata: dict[str, Any] | None = None,
    artifact_type: AgentArtifactType = AgentArtifactType.YUPPASTE,
    blob_store: BlobStore | None = None,
) -> AgentArtifact:
    """Create a textual artifact: upload content + attachments, insert row.

    Returns the inserted :class:`AgentArtifact`. On failure any partially
    uploaded blobs are left in place (operator can reap later — keeps the
    hot path simple and idempotent-enough for a retry).
    """
    if len(content) > MAX_CONTENT_SIZE_BYTES:
        raise ArtifactError(f"Content exceeds {MAX_CONTENT_SIZE_BYTES} bytes")
    attachments = attachments or []
    if len(attachments) > MAX_ATTACHMENTS_PER_ARTIFACT:
        raise ArtifactError(f"Too many attachments (>{MAX_ATTACHMENTS_PER_ARTIFACT})")

    # 1. Resolve version (may raise).
    version = await _resolve_next_version(slug=named_slug, create_new_slug=create_new_slug, artifact_type=artifact_type)

    # 2. Generate ID + upload blobs.
    artifact_id = uuid.uuid4()
    store = blob_store or get_blob_store()
    main_path = content_path_for(artifact_id, content_type)
    await store.upload(main_path, content, content_type)

    attachment_infos: list[dict[str, Any]] = []
    for att in attachments:
        att_path = attachment_path_for(artifact_id, att.filename)
        await store.upload(att_path, att.data, att.content_type)
        attachment_infos.append(
            {
                "filename": att.filename,
                "content_type": att.content_type,
                "size_bytes": len(att.data),
            }
        )

    # 3. Insert metadata row.
    metadata: dict[str, Any] = {
        "attachments": attachment_infos,
        "is_archived": False,
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    artifact = AgentArtifact(
        agent_artifact_id=artifact_id,
        artifact_type=artifact_type,
        title=title,
        description=description,
        url=f"/ahs/artifacts/{artifact_id}",
        creator_user_id=creator_user_id,
        creator_agent_id=creator_agent_id,
        agent_session_id=agent_session_id,
        agent_task_id=agent_task_id,
        content_type=content_type,
        named_slug=named_slug,
        version=version,
        artifact_metadata=metadata,
    )
    return await _insert_artifact_row(artifact)


@retry_db
async def get_artifact_by_id(artifact_id: uuid.UUID) -> AgentArtifact | None:
    async with get_async_session_read_replica() as session:
        stmt = select(AgentArtifact).where(
            AgentArtifact.agent_artifact_id == artifact_id,
            col(AgentArtifact.deleted_at).is_(None),
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


@retry_db
async def get_artifact_by_slug(
    slug: str,
    version: int | None = None,
    artifact_type: AgentArtifactType = AgentArtifactType.YUPPASTE,
) -> AgentArtifact | None:
    """Resolve ``slug`` to an artifact row.

    When ``version`` is None, returns the latest non-archived version.
    """
    async with get_async_session_read_replica() as session:
        stmt = select(AgentArtifact).where(
            AgentArtifact.named_slug == slug,
            AgentArtifact.artifact_type == artifact_type,
            col(AgentArtifact.deleted_at).is_(None),
        )
        if version is not None:
            stmt = stmt.where(AgentArtifact.version == version)
        else:
            stmt = stmt.where(
                text("(agent_artifacts.artifact_metadata->>'is_archived') IS DISTINCT FROM 'true'")
            ).order_by(col(AgentArtifact.version).desc())
        stmt = stmt.limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


@retry_db
async def list_artifact_versions(
    slug: str,
    artifact_type: AgentArtifactType = AgentArtifactType.YUPPASTE,
) -> list[AgentArtifact]:
    async with get_async_session_read_replica() as session:
        stmt = (
            select(AgentArtifact)
            .where(
                AgentArtifact.named_slug == slug,
                AgentArtifact.artifact_type == artifact_type,
                col(AgentArtifact.deleted_at).is_(None),
            )
            .order_by(col(AgentArtifact.version).asc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def read_artifact_content(
    artifact: AgentArtifact,
    blob_store: BlobStore | None = None,
) -> tuple[bytes, str]:
    """Return ``(content_bytes, content_type)`` for an artifact's main file."""
    if artifact.content_type is None:
        raise ArtifactError(
            f"Artifact {artifact.agent_artifact_id} has no content_type set — "
            "it may be a metadata-only artifact (e.g., CODE_REVIEW)."
        )
    store = blob_store or get_blob_store()
    path = content_path_for(artifact.agent_artifact_id, artifact.content_type)
    data = await store.download(path)
    return data, artifact.content_type


async def read_artifact_attachment(
    artifact: AgentArtifact,
    filename: str,
    blob_store: BlobStore | None = None,
) -> tuple[bytes, str]:
    """Return ``(bytes, content_type)`` for an attachment on an artifact."""
    # Look up the recorded attachment so we return the right content_type.
    metadata = artifact.artifact_metadata or {}
    attachments = metadata.get("attachments", []) or []
    match = next(
        (a for a in attachments if a.get("filename") == filename),
        None,
    )
    if match is None:
        raise ArtifactError(f"Attachment {filename!r} not found on artifact {artifact.agent_artifact_id}")
    store = blob_store or get_blob_store()
    data = await store.download(attachment_path_for(artifact.agent_artifact_id, filename))
    return data, str(match.get("content_type", "application/octet-stream"))


@retry_db
async def archive_artifact(artifact_id: uuid.UUID) -> bool:
    """Mark an artifact as archived. Blobs are kept (cheap; cleaned by a sweep)."""
    async with get_async_session() as session:
        artifact = await session.get(AgentArtifact, artifact_id)
        if artifact is None or artifact.deleted_at is not None:
            return False
        metadata = dict(artifact.artifact_metadata or {})
        metadata["is_archived"] = True
        # JSONB replace via type-aware update so SQLAlchemy emits a proper UPDATE.
        await session.execute(
            AgentArtifact.__table__.update()  # type: ignore[attr-defined]
            .where(AgentArtifact.agent_artifact_id == artifact_id)
            .values(artifact_metadata=metadata)
        )
        await session.commit()
        return True


@retry_db
async def archive_artifacts_by_slug(
    slug: str,
    artifact_type: AgentArtifactType = AgentArtifactType.YUPPASTE,
) -> int:
    """Archive every non-archived version under ``slug``.

    Returns the number of versions that were newly flipped to archived
    (already-archived versions are skipped silently).
    """
    async with get_async_session() as session:
        stmt = select(AgentArtifact).where(
            AgentArtifact.named_slug == slug,
            AgentArtifact.artifact_type == artifact_type,
            col(AgentArtifact.deleted_at).is_(None),
            text("(agent_artifacts.artifact_metadata->>'is_archived') IS DISTINCT FROM 'true'"),
        )
        result = await session.execute(stmt)
        artifacts = list(result.scalars().all())
        for artifact in artifacts:
            metadata = dict(artifact.artifact_metadata or {})
            metadata["is_archived"] = True
            await session.execute(
                AgentArtifact.__table__.update()  # type: ignore[attr-defined]
                .where(AgentArtifact.agent_artifact_id == artifact.agent_artifact_id)
                .values(artifact_metadata=metadata)
            )
        await session.commit()
        return len(artifacts)


# ---------------------------------------------------------------------------
# Listing (non-slug, for admin/search use)
# ---------------------------------------------------------------------------


@retry_db
async def list_artifacts(
    *,
    artifact_type: AgentArtifactType | None = None,
    creator_user_id: str | None = None,
    agent_session_id: uuid.UUID | None = None,
    agent_task_id: uuid.UUID | None = None,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[AgentArtifact]:
    async with get_async_session_read_replica() as session:
        stmt = select(AgentArtifact).where(col(AgentArtifact.deleted_at).is_(None))
        if artifact_type is not None:
            stmt = stmt.where(AgentArtifact.artifact_type == artifact_type)
        if creator_user_id is not None:
            stmt = stmt.where(AgentArtifact.creator_user_id == creator_user_id)
        if agent_session_id is not None:
            stmt = stmt.where(AgentArtifact.agent_session_id == agent_session_id)
        if agent_task_id is not None:
            stmt = stmt.where(AgentArtifact.agent_task_id == agent_task_id)
        if not include_archived:
            stmt = stmt.where(text("(agent_artifacts.artifact_metadata->>'is_archived') IS DISTINCT FROM 'true'"))
        stmt = stmt.order_by(col(AgentArtifact.created_at).desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())


@retry_db
async def search_artifacts(
    query: str,
    *,
    artifact_type: AgentArtifactType | None = None,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[AgentArtifact]:
    """Case-insensitive match over title, description, named_slug, and attachment filenames.

    Matching is substring (``ILIKE '%query%'``) — no tsvector/full-text over
    the content body yet. Results are reverse-chronological.
    """
    q = query.strip()
    if not q:
        return []
    like_pattern = f"%{q}%"
    # JSONB path: match if any attachment record's filename contains the query.
    attachment_filename_match = text(
        "EXISTS ("
        "SELECT 1 FROM jsonb_array_elements("
        "COALESCE(agent_artifacts.artifact_metadata->'attachments', '[]'::jsonb)"
        ") AS att WHERE att->>'filename' ILIKE :q"
        ")"
    ).bindparams(q=like_pattern)

    async with get_async_session_read_replica() as session:
        stmt = select(AgentArtifact).where(col(AgentArtifact.deleted_at).is_(None))
        if artifact_type is not None:
            stmt = stmt.where(AgentArtifact.artifact_type == artifact_type)
        if not include_archived:
            stmt = stmt.where(text("(agent_artifacts.artifact_metadata->>'is_archived') IS DISTINCT FROM 'true'"))
        stmt = stmt.where(
            or_(
                col(AgentArtifact.title).ilike(like_pattern),
                col(AgentArtifact.description).ilike(like_pattern),
                col(AgentArtifact.named_slug).ilike(like_pattern),
                attachment_filename_match,
            )
        )
        stmt = stmt.order_by(col(AgentArtifact.created_at).desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())
