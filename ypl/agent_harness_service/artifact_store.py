"""Service layer for textual artifacts.

Writes artifact content to the configured ``BlobStore`` and metadata to
the ``agent_artifacts`` Postgres table. Loads go through the same two
stores. Default artifact type is ``TEXT`` — content stored inline in
the blob store, siblings are attachments, and metadata (title, slug,
version, creator, session/task linkage) lives in ``agent_artifacts``.

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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, func, or_, text
from sqlmodel import col, select

from ypl.backend.config import settings
from ypl.backend.db import get_async_session, get_async_session_read_replica, retry_db
from ypl.backend.utils.blob_store import BlobStore, get_blob_store
from ypl.db.agent_harness import Agent, AgentArtifact, AgentArtifactType
from ypl.db.users import User
from ypl.structured_logger import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# Memory scoping (MEMORY artifacts only)
# ---------------------------------------------------------------------------

# The three valid values of ``memory_scope`` on a MEMORY artifact row.
MemoryScope = Literal["user", "agent", "topic"]
VALID_MEMORY_SCOPES: frozenset[str] = frozenset({"user", "agent", "topic"})


@dataclass(frozen=True)
class MemoryCallerContext:
    """Caller identity used to filter MEMORY reads / authorize MEMORY writes.

    ``user_id`` is the ``users.user_id`` the caller is acting on behalf of
    (usually from the session's ``requesting_user_id`` / ``X-User-ID`` header).
    ``agent_name`` is the ``agents.name`` the caller is executing as (usually
    from the AHS runner's ``X-AHS-Agent-Name`` header, which is tamper-proof).

    Either may be ``None`` when the caller lacks that identity (e.g. an
    operator script only sets ``X-User-ID``). A caller with neither identity
    set has no MEMORY read access — the read filter returns no rows — which
    makes it impossible to leak cross-user/cross-agent data to an
    unauthenticated caller.
    """

    user_id: str | None = None
    agent_name: str | None = None

    @property
    def has_identity(self) -> bool:
        return self.user_id is not None or self.agent_name is not None


def memory_read_clause(caller: MemoryCallerContext) -> Any:
    """Return a SQLAlchemy clause selecting MEMORY rows readable by ``caller``.

    The resulting clause is ``topic`` ∪ (``user`` ∧ subject = caller.user_id) ∪
    (``agent`` ∧ subject = caller.agent_name). A caller with neither identity
    set matches no rows (conservative default; admin routes should bypass the
    filter instead).
    """
    # Wrap column references in ``col()`` so SQLAlchemy's / mypy's typing
    # treats the comparisons as ``ColumnElement[bool]`` rather than plain
    # ``bool`` — matches how the rest of this module uses ``col()``.
    scope_col = col(AgentArtifact.memory_scope)
    subject_col = col(AgentArtifact.memory_scope_subject)
    branches: list[Any] = [scope_col == "topic"]
    if caller.user_id is not None:
        branches.append(and_(scope_col == "user", subject_col == caller.user_id))
    if caller.agent_name is not None:
        branches.append(and_(scope_col == "agent", subject_col == caller.agent_name))
    return or_(*branches) if len(branches) > 1 else branches[0]


def caller_can_write_memory(
    caller: MemoryCallerContext,
    scope: str,
    subject: str | None,
) -> bool:
    """Return True if ``caller`` is allowed to write to (scope, subject).

    Rules (see design doc §'Access control'):

    - scope=topic     → any authenticated caller.
    - scope=user, X   → caller.user_id must equal X.
    - scope=agent, Y  → caller.agent_name must equal Y.
    """
    if scope == "topic":
        return True
    if scope == "user":
        return caller.user_id is not None and subject == caller.user_id
    if scope == "agent":
        return caller.agent_name is not None and subject == caller.agent_name
    return False


def validate_memory_scope_shape(scope: str, subject: str | None) -> None:
    """Raise :class:`ArtifactError` if (scope, subject) is not a legal pair.

    Mirrors the DB ``ck_agent_artifacts_memory_subject_presence`` check so we
    fail fast in the service layer before we ever hit Postgres.
    """
    if scope not in VALID_MEMORY_SCOPES:
        raise ArtifactError(f"Invalid memory_scope {scope!r}; allowed: {sorted(VALID_MEMORY_SCOPES)}")
    if scope == "topic":
        if subject is not None:
            raise ArtifactError("memory_scope='topic' forbids memory_scope_subject")
    elif not subject:
        raise ArtifactError(f"memory_scope={scope!r} requires memory_scope_subject")


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


def _scope_slug_filter(
    *,
    slug: str,
    artifact_type: AgentArtifactType,
    memory_scope: str | None,
    memory_scope_subject: str | None,
) -> list[Any]:
    """Build the WHERE clauses that locate a slug's versions.

    For MEMORY artifacts uniqueness is (scope, subject, slug, version) — the
    same slug can live under multiple scopes/subjects. Non-MEMORY artifacts
    fall back to global slug uniqueness.
    """
    clauses: list[Any] = [
        AgentArtifact.named_slug == slug,
        AgentArtifact.artifact_type == artifact_type,
    ]
    if artifact_type == AgentArtifactType.MEMORY:
        if memory_scope is None:
            raise ArtifactError("MEMORY slug lookups require memory_scope (and memory_scope_subject for user/agent).")
        clauses.append(AgentArtifact.memory_scope == memory_scope)
        if memory_scope == "topic":
            clauses.append(col(AgentArtifact.memory_scope_subject).is_(None))
        else:
            if not memory_scope_subject:
                raise ArtifactError(
                    f"MEMORY slug lookups with memory_scope={memory_scope!r} require memory_scope_subject."
                )
            clauses.append(AgentArtifact.memory_scope_subject == memory_scope_subject)
    return clauses


async def _max_version_for_slug(
    slug: str,
    artifact_type: AgentArtifactType,
    *,
    memory_scope: str | None = None,
    memory_scope_subject: str | None = None,
) -> int | None:
    """Return the highest existing version for ``slug`` (including archived).

    For MEMORY artifacts the lookup is narrowed to the (scope, subject) tuple
    so different scopes can carry identically-named slugs.
    """
    clauses = _scope_slug_filter(
        slug=slug,
        artifact_type=artifact_type,
        memory_scope=memory_scope,
        memory_scope_subject=memory_scope_subject,
    )
    async with get_async_session_read_replica() as session:
        stmt = select(func.max(AgentArtifact.version)).where(*clauses)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def _slug_has_active_version(
    slug: str,
    artifact_type: AgentArtifactType,
    *,
    memory_scope: str | None = None,
    memory_scope_subject: str | None = None,
) -> bool:
    """True if any non-archived version of ``slug`` exists for the scope tuple."""
    clauses = _scope_slug_filter(
        slug=slug,
        artifact_type=artifact_type,
        memory_scope=memory_scope,
        memory_scope_subject=memory_scope_subject,
    )
    clauses.extend(
        [
            col(AgentArtifact.deleted_at).is_(None),
            # is_archived stored in artifact_metadata JSONB.
            text("(agent_artifacts.artifact_metadata->>'is_archived') IS DISTINCT FROM 'true'"),
        ]
    )
    async with get_async_session_read_replica() as session:
        stmt = select(AgentArtifact.agent_artifact_id).where(*clauses).limit(1)
        result = await session.execute(stmt)
        return result.first() is not None


async def _resolve_next_version(
    *,
    slug: str | None,
    create_new_slug: bool,
    artifact_type: AgentArtifactType,
    memory_scope: str | None = None,
    memory_scope_subject: str | None = None,
) -> int | None:
    """Return the ``version`` to assign to a new artifact, or ``None`` if slug is unset."""
    if slug is None:
        if create_new_slug:
            raise ArtifactError("create_new_slug=True requires named_slug")
        return None

    validate_named_slug(slug)
    existing_max = await _max_version_for_slug(
        slug,
        artifact_type,
        memory_scope=memory_scope,
        memory_scope_subject=memory_scope_subject,
    )
    has_active = await _slug_has_active_version(
        slug,
        artifact_type,
        memory_scope=memory_scope,
        memory_scope_subject=memory_scope_subject,
    )

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
    content: bytes | None = None,
    inline_content: str | None = None,
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
    artifact_type: AgentArtifactType = AgentArtifactType.TEXT,
    memory_scope: str | None = None,
    memory_scope_subject: str | None = None,
    blob_store: BlobStore | None = None,
) -> AgentArtifact:
    """Create an artifact: upload content + attachments (blob) OR store inline, then insert row.

    For ``artifact_type=MEMORY`` pass ``inline_content`` (a str) and the
    ``memory_scope`` + ``memory_scope_subject`` pair — the body is written
    directly to the ``agent_artifacts.inline_content`` column, the blob
    store is skipped entirely, and no canonical ``url`` is generated
    (viewers resolve the row by ID / scope+slug).

    For every other type pass ``content`` bytes — the main blob is uploaded
    to the configured ``BlobStore`` and a canonical viewer URL is stored.

    On failure any partially uploaded blobs are left in place (operator can
    reap later — keeps the hot path simple and idempotent-enough for a retry).
    """
    attachments = attachments or []
    if len(attachments) > MAX_ATTACHMENTS_PER_ARTIFACT:
        raise ArtifactError(f"Too many attachments (>{MAX_ATTACHMENTS_PER_ARTIFACT})")

    is_memory = artifact_type == AgentArtifactType.MEMORY

    if is_memory:
        # MEMORY: inline body; no blobs, no attachments.
        if inline_content is None:
            raise ArtifactError("MEMORY artifacts require inline_content")
        if content is not None:
            raise ArtifactError("MEMORY artifacts must not carry 'content' bytes — use inline_content")
        if attachments:
            raise ArtifactError("MEMORY artifacts do not support attachments")
        if memory_scope is None:
            raise ArtifactError("MEMORY artifacts require memory_scope")
        validate_memory_scope_shape(memory_scope, memory_scope_subject)
        if len(inline_content.encode("utf-8")) > MAX_CONTENT_SIZE_BYTES:
            raise ArtifactError(f"inline_content exceeds {MAX_CONTENT_SIZE_BYTES} bytes")
    else:
        # Non-MEMORY: blob-backed body.
        if inline_content is not None:
            raise ArtifactError(f"inline_content is only valid for MEMORY artifacts (got type={artifact_type.value})")
        if memory_scope is not None or memory_scope_subject is not None:
            raise ArtifactError(
                f"memory_scope / memory_scope_subject only apply to MEMORY artifacts (got type={artifact_type.value})"
            )
        if content is None:
            raise ArtifactError(f"{artifact_type.value} artifacts require 'content' bytes")
        if len(content) > MAX_CONTENT_SIZE_BYTES:
            raise ArtifactError(f"Content exceeds {MAX_CONTENT_SIZE_BYTES} bytes")

    # 1. Resolve version (may raise). For MEMORY the (scope, subject) tuple is
    # part of the slug-uniqueness key; other types use global slug uniqueness.
    version = await _resolve_next_version(
        slug=named_slug,
        create_new_slug=create_new_slug,
        artifact_type=artifact_type,
        memory_scope=memory_scope if is_memory else None,
        memory_scope_subject=memory_scope_subject if is_memory else None,
    )

    # 2. Generate ID + upload blobs (non-MEMORY only).
    artifact_id = uuid.uuid4()
    attachment_infos: list[dict[str, Any]] = []
    url: str | None = None

    if not is_memory:
        assert content is not None  # narrowed above
        store = blob_store or get_blob_store()
        main_path = content_path_for(artifact_id, content_type)
        await store.upload(main_path, content, content_type)

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

        # Canonical shareable URL. Uses the configured viewer base so callers /
        # Slack messages / agents get a human-readable link; falls back to the
        # raw AHS path when no viewer is deployed.
        viewer_base = settings.VIEWER_BASE_URL.rstrip("/")
        url = f"{viewer_base}/artifacts/{artifact_id}" if viewer_base else f"/ahs/artifacts/{artifact_id}"

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
        url=url,
        inline_content=inline_content,
        memory_scope=memory_scope,
        memory_scope_subject=memory_scope_subject,
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
    artifact_type: AgentArtifactType = AgentArtifactType.TEXT,
    *,
    memory_scope: str | None = None,
    memory_scope_subject: str | None = None,
) -> AgentArtifact | None:
    """Resolve ``slug`` to an artifact row.

    When ``version`` is None, returns the latest non-archived version.

    For ``artifact_type=MEMORY`` the (scope, subject) tuple is required and
    the lookup is narrowed accordingly — the same slug can live under
    multiple scopes without collision.
    """
    clauses = _scope_slug_filter(
        slug=slug,
        artifact_type=artifact_type,
        memory_scope=memory_scope,
        memory_scope_subject=memory_scope_subject,
    )
    clauses.append(col(AgentArtifact.deleted_at).is_(None))
    async with get_async_session_read_replica() as session:
        stmt = select(AgentArtifact).where(*clauses)
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
    artifact_type: AgentArtifactType = AgentArtifactType.TEXT,
    *,
    memory_scope: str | None = None,
    memory_scope_subject: str | None = None,
) -> list[AgentArtifact]:
    """Enumerate non-archived versions of ``slug`` in chronological order.

    For ``artifact_type=MEMORY`` scope/subject are required (see
    :func:`get_artifact_by_slug`).
    """
    clauses = _scope_slug_filter(
        slug=slug,
        artifact_type=artifact_type,
        memory_scope=memory_scope,
        memory_scope_subject=memory_scope_subject,
    )
    clauses.append(col(AgentArtifact.deleted_at).is_(None))
    async with get_async_session_read_replica() as session:
        stmt = select(AgentArtifact).where(*clauses).order_by(col(AgentArtifact.version).asc())
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def read_artifact_content(
    artifact: AgentArtifact,
    blob_store: BlobStore | None = None,
) -> tuple[bytes, str]:
    """Return ``(content_bytes, content_type)`` for an artifact's main body.

    MEMORY artifacts with ``inline_content`` resolve directly from the row
    — no blob store round-trip. Any artifact (MEMORY or otherwise) with
    ``inline_content`` set takes the inline path; otherwise the main file
    is fetched from the blob store using the canonical ``content_path_for``
    layout.
    """
    if artifact.inline_content is not None:
        # Inline-content path (MEMORY). content_type may still be NULL in weird
        # rows — default to text/markdown to keep downstream consumers happy.
        content_type = artifact.content_type or "text/markdown"
        return artifact.inline_content.encode("utf-8"), content_type

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
    artifact_type: AgentArtifactType = AgentArtifactType.TEXT,
    *,
    memory_scope: str | None = None,
    memory_scope_subject: str | None = None,
) -> int:
    """Archive every non-archived version under ``slug``.

    Returns the number of versions that were newly flipped to archived
    (already-archived versions are skipped silently). For MEMORY artifacts
    the scope/subject tuple is required — it disambiguates slugs that exist
    in multiple scopes.
    """
    clauses = _scope_slug_filter(
        slug=slug,
        artifact_type=artifact_type,
        memory_scope=memory_scope,
        memory_scope_subject=memory_scope_subject,
    )
    clauses.extend(
        [
            col(AgentArtifact.deleted_at).is_(None),
            text("(agent_artifacts.artifact_metadata->>'is_archived') IS DISTINCT FROM 'true'"),
        ]
    )
    async with get_async_session() as session:
        stmt = select(AgentArtifact).where(*clauses)
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
    creator_agent_id: uuid.UUID | None = None,
    agent_session_id: uuid.UUID | None = None,
    agent_task_id: uuid.UUID | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
    memory_caller: MemoryCallerContext | None = None,
    memory_scope: str | None = None,
    memory_scope_subject: str | None = None,
) -> list[AgentArtifact]:
    """List artifacts matching the given filters.

    MEMORY-scoped filtering:

    - ``memory_caller`` — if set, non-NULL MEMORY rows are additionally
      constrained to the caller's visibility (``topic`` + own user + own
      agent). Non-MEMORY rows are unaffected. When unset, no MEMORY
      filtering is applied (admin / viewer access).
    - ``memory_scope`` / ``memory_scope_subject`` — if set, narrow the
      MEMORY rows to a specific scope/subject. Non-MEMORY rows are excluded
      from the response when either is set because scope columns are NULL
      for those rows.
    """
    async with get_async_session_read_replica() as session:
        stmt = select(AgentArtifact).where(col(AgentArtifact.deleted_at).is_(None))
        if artifact_type is not None:
            stmt = stmt.where(AgentArtifact.artifact_type == artifact_type)
        if creator_user_id is not None:
            stmt = stmt.where(AgentArtifact.creator_user_id == creator_user_id)
        if creator_agent_id is not None:
            stmt = stmt.where(AgentArtifact.creator_agent_id == creator_agent_id)
        if agent_session_id is not None:
            stmt = stmt.where(AgentArtifact.agent_session_id == agent_session_id)
        if agent_task_id is not None:
            stmt = stmt.where(AgentArtifact.agent_task_id == agent_task_id)
        if created_after is not None:
            stmt = stmt.where(col(AgentArtifact.created_at) >= created_after)
        if created_before is not None:
            stmt = stmt.where(col(AgentArtifact.created_at) < created_before)
        if not include_archived:
            stmt = stmt.where(text("(agent_artifacts.artifact_metadata->>'is_archived') IS DISTINCT FROM 'true'"))
        stmt = _apply_memory_filters(
            stmt,
            memory_caller=memory_caller,
            memory_scope=memory_scope,
            memory_scope_subject=memory_scope_subject,
        )
        stmt = stmt.order_by(col(AgentArtifact.created_at).desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())


def _apply_memory_filters(
    stmt: Any,
    *,
    memory_caller: MemoryCallerContext | None,
    memory_scope: str | None,
    memory_scope_subject: str | None,
) -> Any:
    """Apply the MEMORY visibility + scope/subject narrowing to ``stmt``.

    Non-MEMORY rows are preserved when ``memory_scope`` / ``subject`` are
    unset (they just don't take part in the MEMORY filter); narrowing to a
    specific scope or subject implicitly excludes non-MEMORY rows because
    their scope columns are NULL.
    """
    if memory_caller is not None:
        # Let non-MEMORY rows through unconditionally; constrain MEMORY rows
        # to the caller's visibility.
        stmt = stmt.where(
            or_(
                col(AgentArtifact.artifact_type) != AgentArtifactType.MEMORY,
                memory_read_clause(memory_caller),
            )
        )
    if memory_scope is not None:
        stmt = stmt.where(col(AgentArtifact.memory_scope) == memory_scope)
    if memory_scope_subject is not None:
        stmt = stmt.where(col(AgentArtifact.memory_scope_subject) == memory_scope_subject)
    return stmt


@retry_db
async def count_artifacts(
    *,
    artifact_type: AgentArtifactType | None = None,
    creator_user_id: str | None = None,
    creator_agent_id: uuid.UUID | None = None,
    agent_session_id: uuid.UUID | None = None,
    agent_task_id: uuid.UUID | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    include_archived: bool = False,
    memory_caller: MemoryCallerContext | None = None,
    memory_scope: str | None = None,
    memory_scope_subject: str | None = None,
) -> int:
    """Count artifacts matching the same filters as :func:`list_artifacts`.

    Used by the listing UI to decide whether a "Next" page link should be
    shown without paying the cost of fetching beyond the current page. The
    MEMORY filtering parameters mirror :func:`list_artifacts` so a paginated
    response's ``total`` matches the number of rows the caller can actually
    see.
    """
    async with get_async_session_read_replica() as session:
        stmt = select(func.count(col(AgentArtifact.agent_artifact_id))).where(col(AgentArtifact.deleted_at).is_(None))
        if artifact_type is not None:
            stmt = stmt.where(AgentArtifact.artifact_type == artifact_type)
        if creator_user_id is not None:
            stmt = stmt.where(AgentArtifact.creator_user_id == creator_user_id)
        if creator_agent_id is not None:
            stmt = stmt.where(AgentArtifact.creator_agent_id == creator_agent_id)
        if agent_session_id is not None:
            stmt = stmt.where(AgentArtifact.agent_session_id == agent_session_id)
        if agent_task_id is not None:
            stmt = stmt.where(AgentArtifact.agent_task_id == agent_task_id)
        if created_after is not None:
            stmt = stmt.where(col(AgentArtifact.created_at) >= created_after)
        if created_before is not None:
            stmt = stmt.where(col(AgentArtifact.created_at) < created_before)
        if not include_archived:
            stmt = stmt.where(text("(agent_artifacts.artifact_metadata->>'is_archived') IS DISTINCT FROM 'true'"))
        stmt = _apply_memory_filters(
            stmt,
            memory_caller=memory_caller,
            memory_scope=memory_scope,
            memory_scope_subject=memory_scope_subject,
        )
        result = await session.execute(stmt)
        return int(result.scalar_one() or 0)


@retry_db
async def search_artifacts(
    query: str,
    *,
    artifact_type: AgentArtifactType | None = None,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
    memory_caller: MemoryCallerContext | None = None,
    memory_scope: str | None = None,
    memory_scope_subject: str | None = None,
) -> list[AgentArtifact]:
    """Case-insensitive match over title, description, named_slug, inline_content,
    and attachment filenames.

    Matching is substring (``ILIKE '%query%'``) — no tsvector/full-text over
    the content body yet. Results are reverse-chronological.

    MEMORY filtering follows :func:`list_artifacts` — see that docstring for
    the semantics of ``memory_caller`` / ``memory_scope`` / subject.
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
                col(AgentArtifact.inline_content).ilike(like_pattern),
                attachment_filename_match,
            )
        )
        stmt = _apply_memory_filters(
            stmt,
            memory_caller=memory_caller,
            memory_scope=memory_scope,
            memory_scope_subject=memory_scope_subject,
        )
        stmt = stmt.order_by(col(AgentArtifact.created_at).desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Attribution resolution
# ---------------------------------------------------------------------------


@retry_db
async def resolve_attribution(
    artifacts: list[AgentArtifact],
) -> tuple[dict[str, str], dict[uuid.UUID, str]]:
    """Bulk-resolve creator display names for a batch of artifacts.

    Returns ``(user_id → user.name, agent_id → agent.display_name)``.
    Missing IDs and NULL names are simply absent from the returned maps —
    callers fall back to the raw ID / a dash.

    Single round-trip for each table; safe to call with an empty list.
    """
    user_ids = {a.creator_user_id for a in artifacts if a.creator_user_id}
    agent_ids = {a.creator_agent_id for a in artifacts if a.creator_agent_id}
    if not user_ids and not agent_ids:
        return {}, {}

    user_names: dict[str, str] = {}
    agent_names: dict[uuid.UUID, str] = {}

    async with get_async_session_read_replica() as session:
        if user_ids:
            rows = (await session.execute(select(User.user_id, User.name).where(col(User.user_id).in_(user_ids)))).all()
            user_names = {uid: name for uid, name in rows if name}
        if agent_ids:
            rows = (
                await session.execute(
                    select(Agent.agent_id, Agent.display_name, Agent.name).where(col(Agent.agent_id).in_(agent_ids))
                )
            ).all()
            # Prefer display_name; fall back to unique `name`.
            agent_names = {aid: (display or name) for aid, display, name in rows if (display or name)}

    return user_names, agent_names


@retry_db
async def list_distinct_creators(
    *,
    include_archived: bool = False,
) -> tuple[list[tuple[str, str | None]], list[tuple[uuid.UUID, str | None]]]:
    """Return ``(user_id, name)`` and ``(agent_id, display_name)`` pairs for filter dropdowns.

    Two different policies for the two dropdowns:

    * **Users**: only those who have actually created at least one artifact.
      The user dropdown is bounded by "people who have ever published" and
      that list stays short, so we don't pre-list every user in the system.
    * **Agents**: every non-deleted agent in the agents table — regardless
      of whether it has any artifacts yet. This keeps the dropdown useful
      while the agent attribution backfill is still in progress (we have
      29 registered agents but none have been attributed on existing
      artifacts), and the registered-agent set is small enough that listing
      them all isn't noisy.

    Names come from the ``users`` / ``agents`` tables — IDs whose rows are
    missing or have NULL names are returned with ``name=None`` so callers
    can fall back to the raw ID. Results are sorted alphabetically by name
    (then by ID for the rare missing-name case).
    """
    async with get_async_session_read_replica() as session:
        # Distinct creator_user_id from artifacts, joined to users for names.
        user_stmt = (
            select(AgentArtifact.creator_user_id, User.name)
            .outerjoin(User, col(User.user_id) == col(AgentArtifact.creator_user_id))
            .where(
                col(AgentArtifact.deleted_at).is_(None),
                col(AgentArtifact.creator_user_id).is_not(None),
            )
        )
        if not include_archived:
            user_stmt = user_stmt.where(
                text("(agent_artifacts.artifact_metadata->>'is_archived') IS DISTINCT FROM 'true'")
            )
        user_stmt = user_stmt.distinct()
        user_rows = (await session.execute(user_stmt)).all()
        users: list[tuple[str, str | None]] = sorted(
            ((uid, name) for uid, name in user_rows if uid),
            key=lambda r: ((r[1] or "").lower(), r[0]),
        )

        # All non-deleted agents — no join to artifacts. Keeps the dropdown
        # useful even before agent attribution is wired up everywhere.
        agent_stmt = select(Agent.agent_id, Agent.display_name, Agent.name).where(col(Agent.deleted_at).is_(None))
        agent_rows = (await session.execute(agent_stmt)).all()
        agents: list[tuple[uuid.UUID, str | None]] = sorted(
            ((aid, (display or name)) for aid, display, name in agent_rows if aid),
            key=lambda r: ((r[1] or "").lower(), str(r[0])),
        )

    return users, agents
