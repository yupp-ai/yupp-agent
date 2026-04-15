"""Yuppaste backend — async Postgres (agent_artifacts) + blob_store.

Replaces the previous BigQuery + direct-GCS implementation.  All metadata is
persisted to the ``agent_artifacts`` table via async SQLModel queries; all
content (main file and attachments) is stored through the pluggable BlobStore
abstraction (GCS in production, local filesystem in development).

Blob-store path layout:
    pastes/{uuid[:2]}/{uuid}/{uuid}.{ext}       ← main content file
    pastes/{uuid[:2]}/{uuid}/{filename}          ← attachment (sibling)

Archiving maps to ``deleted_at IS NOT NULL`` (soft-delete via BaseModel).
Attachment metadata is stored in ``artifact_metadata`` JSONB.
"""

import asyncio
import logging
import os
import re
import uuid as _uuid_module
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import asc, desc, func
from sqlmodel import select

from ypl.backend.db import get_async_session_for
from ypl.backend.internal_tools.yuppaste_types import (
    AttachmentInfo,
    YuppasteContentResponse,
    YuppasteCreateResponse,
    YuppasteCreateWithAttachmentsResponse,
    YuppasteListResponse,
    YuppasteMetadata,
    validate_named_slug,
)
from ypl.backend.utils.blob_store import get_blob_store
from ypl.backend.utils.json import json_dumps
from ypl.db.agent_harness import AgentArtifact, AgentArtifactType

MAX_RESPONSE_CONTENT_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

ALLOWED_IMAGE_CONTENT_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/svg+xml",
    }
)
MAX_ATTACHMENT_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB per attachment
MAX_ATTACHMENTS_PER_PASTE = 20
ATTACHMENT_PLACEHOLDER_PATTERN = re.compile(r"!\[([^\]]*)\]\(attachment:([^)]+)\)")

# Characters not allowed in attachment filenames
_INVALID_FILENAME_CHARS = frozenset({"/", "\\", "\x00"})
_INVALID_FILENAMES = frozenset({".", "..", ""})

# Maps MIME content-type → file extension used in blob-store paths.
# Must stay in sync with the CHECK constraint on agent_artifacts.content_type.
YUPPASTE_CONTENT_TYPES: dict[str, str] = {
    "text/plain": "txt",
    "text/markdown": "md",
    "text/html": "html",
}

_DEFAULT_EXT = "txt"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _content_path(file_uuid: str, ext: str) -> str:
    """Return the blob-store logical path for a paste's main content file."""
    return f"pastes/{file_uuid[:2]}/{file_uuid}/{file_uuid}.{ext}"


def _attachment_path(file_uuid: str, filename: str) -> str:
    """Return the blob-store logical path for an attachment (sibling of main file)."""
    return f"pastes/{file_uuid[:2]}/{file_uuid}/{filename}"


def _sanitize_attachment_filename(filename: str) -> str:
    """Sanitize an attachment filename to prevent path traversal.

    Strips to basename and rejects filenames with path separators or special names.
    """
    filename = os.path.basename(filename)
    if filename in _INVALID_FILENAMES or any(c in filename for c in _INVALID_FILENAME_CHARS):
        raise ValueError(f"Invalid attachment filename: {filename!r}")
    return filename


# ---------------------------------------------------------------------------
# Link generators (public API, callers import these)
# ---------------------------------------------------------------------------


def generate_yuppaste_link(paste_uuid: str) -> str:
    """Generate go-link for a Yuppaste UUID."""
    return f"http://go/p/{paste_uuid}"


def generate_yuppaste_slug_link(named_slug: str, version: int | None = None) -> str:
    """Generate go-link for a named Yuppaste slug."""
    if version is not None:
        return f"http://go/p/{named_slug}@{version}"
    return f"http://go/p/{named_slug}"


# ---------------------------------------------------------------------------
# Internal conversion helpers
# ---------------------------------------------------------------------------


def _artifact_to_metadata(artifact: AgentArtifact) -> YuppasteMetadata:
    """Convert an AgentArtifact ORM row to a YuppasteMetadata response model."""
    meta: dict[str, Any] = artifact.artifact_metadata or {}
    content_path: str = meta.get("content_path", "")
    return YuppasteMetadata(
        uuid=str(artifact.agent_artifact_id),
        name=artifact.title or None,
        created_by=artifact.creator_user_id or "",
        content_url=content_path,
        created_at=artifact.created_at or datetime.now(UTC),
        named_slug=artifact.named_slug,
        version=artifact.version,
        is_archived=artifact.deleted_at is not None,
    )


def _meta_to_attachments(meta: dict[str, Any]) -> list[AttachmentInfo]:
    """Extract attachment info list from artifact_metadata JSONB."""
    raw = meta.get("attachments")
    if not raw or not isinstance(raw, list):
        return []
    result: list[AttachmentInfo] = []
    for item in raw:
        try:
            result.append(AttachmentInfo(**item))
        except Exception:
            logging.warning("Skipping malformed attachment metadata: %r", item)
    return result


# ---------------------------------------------------------------------------
# Slug versioning helpers
# ---------------------------------------------------------------------------


async def get_max_version_for_slug(named_slug: str) -> int | None:
    """Get the maximum version number for a named slug.

    Includes archived versions to prevent version-number reuse.

    Args:
        named_slug: The slug to query.

    Returns:
        Maximum version number (including archived), or None if slug never existed.
    """
    async with get_async_session_for("agentdb") as session:
        result = await session.execute(
            select(func.max(AgentArtifact.version))
            .where(AgentArtifact.named_slug == named_slug)
            .where(AgentArtifact.artifact_type == AgentArtifactType.YUPPASTE)
        )
        row = result.one_or_none()
        if row is None:
            return None
        val = row[0]
        return int(val) if val is not None else None


async def slug_exists(named_slug: str) -> bool:
    """Return True if the slug has at least one active (non-archived) version.

    Args:
        named_slug: The slug to check.

    Returns:
        True if the slug exists with at least one active version.
    """
    async with get_async_session_for("agentdb") as session:
        result = await session.exec(
            select(AgentArtifact.agent_artifact_id)
            .where(AgentArtifact.named_slug == named_slug)
            .where(AgentArtifact.artifact_type == AgentArtifactType.YUPPASTE)
            .where(AgentArtifact.deleted_at.is_(None))  # type: ignore[union-attr]
            .limit(1)
        )
        return result.first() is not None


# ---------------------------------------------------------------------------
# Versioning logic (shared by create functions)
# ---------------------------------------------------------------------------


async def _resolve_version(named_slug: str, create_new_slug: bool) -> int:
    """Determine the next version number for a named slug.

    Raises:
        ValueError: If the slug state contradicts ``create_new_slug``.
    """
    exists = await slug_exists(named_slug)
    if create_new_slug:
        if exists:
            raise ValueError(f"named_slug '{named_slug}' already exists")
    else:
        if not exists:
            raise ValueError(f"named_slug '{named_slug}' does not exist. Use create_new_slug=True to create it.")
    max_version = await get_max_version_for_slug(named_slug)
    return (max_version or 0) + 1


# ---------------------------------------------------------------------------
# List / metadata
# ---------------------------------------------------------------------------


async def get_pastes_metadata_from_bigquery(
    page: int,
    page_size: int,
    created_by: str | None,
    sort_by: str,
    sort_order: str,
    named_slug: str | None = None,
    include_archived: bool = False,
) -> YuppasteListResponse:
    """Return a paginated list of yuppaste metadata from Postgres.

    Function name kept for caller compatibility (was BigQuery-backed).
    """
    offset = (page - 1) * page_size

    # Build a reusable base filter
    def _base_stmt() -> Any:
        stmt: Any = select(AgentArtifact).where(AgentArtifact.artifact_type == AgentArtifactType.YUPPASTE)
        if created_by:
            stmt = stmt.where(AgentArtifact.creator_user_id == created_by)
        if named_slug:
            stmt = stmt.where(AgentArtifact.named_slug == named_slug)
        if not include_archived:
            stmt = stmt.where(AgentArtifact.deleted_at.is_(None))  # type: ignore[union-attr]
        return stmt

    # Determine sort expression
    sort_col_attr: Any = AgentArtifact.created_at
    if sort_by == "created_by":
        sort_col_attr = AgentArtifact.creator_user_id
    elif sort_by == "name":
        sort_col_attr = AgentArtifact.title

    order_expr = desc(sort_col_attr) if sort_order == "desc" else asc(sort_col_attr)

    try:
        async with get_async_session_for("agentdb") as session:
            # Total count — reuse the same filter logic as _base_stmt()
            count_stmt: Any = (
                select(func.count())
                .select_from(AgentArtifact)
                .where(AgentArtifact.artifact_type == AgentArtifactType.YUPPASTE)
            )
            if created_by:
                count_stmt = count_stmt.where(AgentArtifact.creator_user_id == created_by)
            if named_slug:
                count_stmt = count_stmt.where(AgentArtifact.named_slug == named_slug)
            if not include_archived:
                count_stmt = count_stmt.where(AgentArtifact.deleted_at.is_(None))  # type: ignore[union-attr]
            count_result = await session.execute(count_stmt)
            total_count: int = count_result.scalar() or 0

            # Data page
            data_stmt = _base_stmt().order_by(order_expr).offset(offset).limit(page_size)
            data_result = await session.exec(data_stmt)
            artifacts = data_result.all()

    except Exception as e:
        logging.error(
            json_dumps({"message": "Postgres error during yuppaste metadata retrieval", "created_by": created_by}),
            exc_info=True,
        )
        raise ValueError("Failed to retrieve yuppaste metadata.") from e

    pastes = [_artifact_to_metadata(a) for a in artifacts]
    has_next = (page * page_size) < total_count
    has_previous = page > 1

    return YuppasteListResponse(
        pastes=pastes,
        total_count=total_count,
        page=page,
        page_size=page_size,
        has_next=has_next,
        has_previous=has_previous,
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def create_yuppaste(
    data: str,
    created_by: str,
    name: str | None = None,
    content_type: str = "text/plain",
    named_slug: str | None = None,
    create_new_slug: bool = False,
) -> YuppasteCreateResponse:
    """Create a new yuppaste with the given data.

    Args:
        data: Text content of the paste.
        created_by: Email of the creator.
        name: Optional name/title.
        content_type: MIME type for the content (default: text/plain).
        named_slug: Optional URL-safe slug for named pastes.
        create_new_slug: True to create a new slug (version 1), False to add version to existing.

    Returns:
        Response with paste metadata.

    Raises:
        ValueError: If slug validation fails or slug exists/doesn't exist as expected.
    """
    version: int | None = None
    if named_slug:
        validate_named_slug(named_slug)
        version = await _resolve_version(named_slug, create_new_slug)

    file_uuid = str(_uuid_module.uuid4())
    ext = YUPPASTE_CONTENT_TYPES.get(content_type, _DEFAULT_EXT)
    path = _content_path(file_uuid, ext)
    created_at = datetime.now(UTC)

    # Upload content to blob store
    blob_store = get_blob_store()
    await blob_store.upload(path, data.encode("utf-8"), content_type)

    # Persist metadata to Postgres
    stored_content_type = content_type if content_type in YUPPASTE_CONTENT_TYPES else None
    artifact = AgentArtifact(
        agent_artifact_id=_uuid_module.UUID(file_uuid),
        artifact_type=AgentArtifactType.YUPPASTE,
        title=name or "",
        url=generate_yuppaste_link(file_uuid),
        creator_user_id=created_by,
        named_slug=named_slug,
        version=version,
        content_type=stored_content_type,
        artifact_metadata={"content_path": path},
        created_at=created_at,
    )
    async with get_async_session_for("agentdb") as session:
        session.add(artifact)
        await session.commit()
        await session.refresh(artifact)

    return YuppasteCreateResponse(
        uuid=file_uuid,
        name=name,
        content_url=path,
        created_at=artifact.created_at or created_at,
        named_slug=named_slug,
        version=version,
        is_archived=False,
    )


def _replace_attachment_placeholders(data: str, file_uuid: str, filenames: set[str]) -> str:
    """Replace attachment placeholders with blob-store paths.

    Converts ``![alt](attachment:filename)`` to
    ``![alt](pastes/{uuid[:2]}/{uuid}/{filename})``.
    """

    def _replacer(match: re.Match[str]) -> str:
        alt_text = match.group(1)
        filename = match.group(2)
        if filename in filenames:
            return f"![{alt_text}]({_attachment_path(file_uuid, filename)})"
        return match.group(0)

    return ATTACHMENT_PLACEHOLDER_PATTERN.sub(_replacer, data)


async def create_yuppaste_with_attachments(
    data: str,
    created_by: str,
    name: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
    content_type: str = "text/plain",
    named_slug: str | None = None,
    create_new_slug: bool = False,
) -> YuppasteCreateWithAttachmentsResponse:
    """Create a new yuppaste with optional image attachments.

    Args:
        data: Text content of the paste.
        created_by: Email of the creator.
        name: Optional name/title.
        attachments: List of (filename, content_bytes, content_type) tuples.
        content_type: MIME type for the main content (default: text/plain).
        named_slug: Optional URL-safe slug for named pastes.
        create_new_slug: True to create a new slug (version 1), False to add version to existing.

    Returns:
        Response with paste metadata and attachment info.

    Raises:
        ValueError: If slug validation fails or slug exists/doesn't exist as expected.
    """
    version: int | None = None
    if named_slug:
        validate_named_slug(named_slug)
        version = await _resolve_version(named_slug, create_new_slug)

    attachments = attachments or []

    if len(attachments) > MAX_ATTACHMENTS_PER_PASTE:
        raise ValueError(f"Too many attachments: {len(attachments)} (max {MAX_ATTACHMENTS_PER_PASTE})")

    # Sanitize filenames and validate attachment content
    attachments = [(_sanitize_attachment_filename(fn), content, ct) for fn, content, ct in attachments]

    # Reject duplicate filenames
    seen_filenames: set[str] = set()
    for fn, _, _ in attachments:
        if fn in seen_filenames:
            raise ValueError(f"Duplicate attachment filename: {fn!r}")
        seen_filenames.add(fn)

    for filename, content, att_content_type in attachments:
        if att_content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
            raise ValueError(f"Unsupported content type for '{filename}': {att_content_type}")
        if len(content) > MAX_ATTACHMENT_SIZE_BYTES:
            max_mb = MAX_ATTACHMENT_SIZE_BYTES // (1024 * 1024)
            raise ValueError(f"Attachment '{filename}' exceeds maximum size of {max_mb}MB")

    file_uuid = str(_uuid_module.uuid4())
    ext = YUPPASTE_CONTENT_TYPES.get(content_type, _DEFAULT_EXT)
    main_path = _content_path(file_uuid, ext)
    created_at = datetime.now(UTC)

    blob_store = get_blob_store()

    # Replace placeholders in content before uploading
    attachment_infos: list[AttachmentInfo] = []
    if attachments:
        filenames = {fn for fn, _, _ in attachments}
        data = _replace_attachment_placeholders(data, file_uuid, filenames)

        async def _upload_attachment(filename: str, att_data: bytes, att_ctype: str) -> AttachmentInfo:
            att_path = _attachment_path(file_uuid, filename)
            await blob_store.upload(att_path, att_data, att_ctype)
            return AttachmentInfo(
                filename=filename,
                content_url=att_path,
                content_type=att_ctype,
                size_bytes=len(att_data),
            )

        attachment_infos = list(
            await asyncio.gather(*[_upload_attachment(fn, content, ct) for fn, content, ct in attachments])
        )

    # Upload main content
    await blob_store.upload(main_path, data.encode("utf-8"), content_type)

    # Build artifact_metadata
    artifact_meta: dict[str, Any] = {"content_path": main_path}
    if attachment_infos:
        artifact_meta["attachments"] = [ai.model_dump() for ai in attachment_infos]

    stored_content_type = content_type if content_type in YUPPASTE_CONTENT_TYPES else None
    artifact = AgentArtifact(
        agent_artifact_id=_uuid_module.UUID(file_uuid),
        artifact_type=AgentArtifactType.YUPPASTE,
        title=name or "",
        url=generate_yuppaste_link(file_uuid),
        creator_user_id=created_by,
        named_slug=named_slug,
        version=version,
        content_type=stored_content_type,
        artifact_metadata=artifact_meta,
        created_at=created_at,
    )
    async with get_async_session_for("agentdb") as session:
        session.add(artifact)
        await session.commit()
        await session.refresh(artifact)

    return YuppasteCreateWithAttachmentsResponse(
        uuid=file_uuid,
        name=name,
        content_url=main_path,
        created_at=artifact.created_at or created_at,
        attachments=attachment_infos,
        named_slug=named_slug,
        version=version,
        is_archived=False,
    )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def _fetch_content(content_path: str, paste_uuid: str) -> tuple[str | None, str | None, int]:
    """Download paste content from the blob store.

    Returns:
        (content_str, redirect_url, file_size):
        - If the file is within the inline size limit: (content, None, size)
        - If the file is too large: (None, redirect_url, size)

    Raises:
        ValueError: If the blob does not exist.
    """
    blob_store = get_blob_store()
    try:
        file_size = await blob_store.get_size(content_path)
    except FileNotFoundError:
        raise ValueError(f"Yuppaste content not found for UUID {paste_uuid}") from None

    if file_size > MAX_RESPONSE_CONTENT_SIZE_BYTES:
        redirect_url = await blob_store.get_access_url(content_path)
        return None, redirect_url, file_size

    raw = await blob_store.download(content_path)
    return raw.decode("utf-8"), None, file_size


async def get_yuppaste_by_uuid(paste_uuid: str) -> YuppasteContentResponse:
    """Get yuppaste content and metadata by UUID."""
    async with get_async_session_for("agentdb") as session:
        artifact = await session.get(AgentArtifact, _uuid_module.UUID(paste_uuid))

    if artifact is None:
        raise ValueError(f"Yuppaste with UUID {paste_uuid} not found")

    meta: dict[str, Any] = artifact.artifact_metadata or {}
    content_path: str = meta.get("content_path", "")
    if not content_path:
        raise ValueError(f"Yuppaste {paste_uuid} has no content path in metadata")

    attachment_infos = _meta_to_attachments(meta)
    content, redirect_url, file_size = await _fetch_content(content_path, paste_uuid)

    return YuppasteContentResponse(
        uuid=paste_uuid,
        name=artifact.title or None,
        data=content,
        created_by=artifact.creator_user_id or "",
        content_url=content_path,
        redirect_url=redirect_url,
        file_size=file_size,
        created_at=artifact.created_at or datetime.now(UTC),
        attachments=attachment_infos,
        named_slug=artifact.named_slug,
        version=artifact.version,
        is_archived=artifact.deleted_at is not None,
    )


async def get_yuppaste_by_slug(named_slug: str, version: int | None = None) -> YuppasteContentResponse:
    """Get yuppaste content and metadata by named slug.

    Args:
        named_slug: The slug to look up.
        version: Optional specific version. If None, returns the latest non-archived version.

    Returns:
        YuppasteContentResponse with paste content and metadata.

    Raises:
        ValueError: If no paste found with the given slug/version.
    """
    async with get_async_session_for("agentdb") as session:
        if version is not None:
            stmt = (
                select(AgentArtifact)
                .where(AgentArtifact.named_slug == named_slug)
                .where(AgentArtifact.version == version)
                .where(AgentArtifact.artifact_type == AgentArtifactType.YUPPASTE)
            )
        else:
            stmt = (
                select(AgentArtifact)
                .where(AgentArtifact.named_slug == named_slug)
                .where(AgentArtifact.artifact_type == AgentArtifactType.YUPPASTE)
                .where(AgentArtifact.deleted_at.is_(None))  # type: ignore[union-attr]
                .order_by(desc(AgentArtifact.version))  # type: ignore[arg-type]
                .limit(1)
            )
        result = await session.exec(stmt)
        artifact = result.first()

    if artifact is None:
        if version is not None:
            raise ValueError(f"Yuppaste with slug '{named_slug}' version {version} not found")
        raise ValueError(f"Yuppaste with slug '{named_slug}' not found")

    paste_uuid = str(artifact.agent_artifact_id)
    meta: dict[str, Any] = artifact.artifact_metadata or {}
    content_path: str = meta.get("content_path", "")
    if not content_path:
        raise ValueError(f"Yuppaste {paste_uuid} has no content path in metadata")

    attachment_infos = _meta_to_attachments(meta)
    content, redirect_url, file_size = await _fetch_content(content_path, paste_uuid)

    return YuppasteContentResponse(
        uuid=paste_uuid,
        name=artifact.title or None,
        data=content,
        created_by=artifact.creator_user_id or "",
        content_url=content_path,
        redirect_url=redirect_url,
        file_size=file_size,
        created_at=artifact.created_at or datetime.now(UTC),
        attachments=attachment_infos,
        named_slug=artifact.named_slug,
        version=artifact.version,
        is_archived=artifact.deleted_at is not None,
    )


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


async def archive_yuppaste(paste_uuid: str, archived_by: str) -> YuppasteMetadata:
    """Archive a yuppaste by UUID.

    Sets deleted_at to mark the paste as archived. Archived pastes:
    - Are excluded from default list queries (unless include_archived=True).
    - Are excluded when resolving a slug without a specific version.
    - Can still be accessed directly by UUID or by slug@version.
    - Preserve their version number (prevents version reuse).

    Args:
        paste_uuid: UUID of the paste to archive.
        archived_by: Email of the user archiving the paste.

    Returns:
        Updated metadata of the archived paste (is_archived=True).

    Raises:
        ValueError: If paste not found.
        PermissionError: If user is not the creator.
    """
    async with get_async_session_for("agentdb") as session:
        artifact = await session.get(AgentArtifact, _uuid_module.UUID(paste_uuid))
        if artifact is None:
            raise ValueError(f"Yuppaste with UUID {paste_uuid} not found")
        if artifact.creator_user_id != archived_by:
            raise PermissionError("You can only archive yuppastes created by yourself")
        artifact.deleted_at = datetime.now(UTC)
        session.add(artifact)
        await session.commit()
        await session.refresh(artifact)

    return _artifact_to_metadata(artifact)


async def archive_yuppaste_by_slug(named_slug: str, version: int | None, archived_by: str) -> int:
    """Archive yuppaste(s) by named slug.

    Args:
        named_slug: The slug to archive.
        version: Specific version to archive. If None, archives all active versions.
        archived_by: Email of the user archiving.

    Returns:
        Number of pastes archived.

    Raises:
        PermissionError: If archiving a specific version not owned by archived_by.
    """
    async with get_async_session_for("agentdb") as session:
        # Build fetch statement
        stmt = (
            select(AgentArtifact)
            .where(AgentArtifact.named_slug == named_slug)
            .where(AgentArtifact.artifact_type == AgentArtifactType.YUPPASTE)
            .where(AgentArtifact.deleted_at.is_(None))  # type: ignore[union-attr]
        )
        if version is not None:
            stmt = stmt.where(AgentArtifact.version == version)

        result = await session.exec(stmt)
        artifacts = result.all()

        if not artifacts:
            return 0

        # Ownership check — only for single-version archive
        if version is not None:
            for art in artifacts:
                if art.creator_user_id != archived_by:
                    raise PermissionError("You can only archive yuppastes created by yourself")

        now = datetime.now(UTC)
        for art in artifacts:
            art.deleted_at = now
            session.add(art)
        await session.commit()

    return len(artifacts)


# ---------------------------------------------------------------------------
# Update metadata
# ---------------------------------------------------------------------------


async def update_yuppaste_metadata(
    paste_uuid: str, name: str | None = None, update_by: str | None = None
) -> YuppasteMetadata:
    """Update yuppaste metadata (currently: name/title only).

    Args:
        paste_uuid: UUID of the paste to update.
        name: New name/title. If None, nothing is updated.
        update_by: If provided, only allow updates by the original creator.

    Returns:
        Updated YuppasteMetadata.

    Raises:
        ValueError: If paste not found or no fields provided.
        PermissionError: If update_by is provided and doesn't match the creator.
    """
    if name is None:
        raise ValueError("No fields provided for update")

    async with get_async_session_for("agentdb") as session:
        artifact = await session.get(AgentArtifact, _uuid_module.UUID(paste_uuid))
        if artifact is None:
            raise ValueError(f"Yuppaste with UUID {paste_uuid} not found")
        if update_by is not None and artifact.creator_user_id != update_by:
            raise PermissionError("You can only update yuppastes created by yourself")

        artifact.title = name
        session.add(artifact)
        await session.commit()
        await session.refresh(artifact)

    return _artifact_to_metadata(artifact)
