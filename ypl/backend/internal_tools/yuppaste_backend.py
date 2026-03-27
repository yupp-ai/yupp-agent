import asyncio
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import aiohttp
from gcloud.aio.storage import Storage
from google.api_core import exceptions as google_exceptions
from google.cloud import bigquery
from ypl.backend.config import settings
from ypl.backend.internal_tools.yuppaste_types import (
    AttachmentInfo,
    YuppasteContentResponse,
    YuppasteCreateResponse,
    YuppasteCreateWithAttachmentsResponse,
    YuppasteListResponse,
    YuppasteMetadata,
    validate_named_slug,
)
from ypl.backend.utils.bigquery_utils import get_bigquery_client
from ypl.backend.utils.json import json_dumps

MAX_RESPONSE_CONTENT_SIZE_BYTES = 10 * 1024 * 1024  # 10MB

ALLOWED_IMAGE_CONTENT_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/svg+xml",
    }
)
MAX_ATTACHMENT_SIZE_BYTES = 10 * 1024 * 1024  # 10MB per attachment
MAX_ATTACHMENTS_PER_PASTE = 20
ATTACHMENT_PLACEHOLDER_PATTERN = re.compile(r"!\[([^\]]*)\]\(attachment:([^)]+)\)")

# Characters not allowed in attachment filenames
_INVALID_FILENAME_CHARS = frozenset({"/", "\\", "\x00"})
_INVALID_FILENAMES = frozenset({".", "..", ""})


def _sanitize_attachment_filename(filename: str) -> str:
    """Sanitize an attachment filename to prevent path traversal.

    Strips to basename and rejects filenames with path separators or special names.
    """
    filename = os.path.basename(filename)
    if filename in _INVALID_FILENAMES or any(c in filename for c in _INVALID_FILENAME_CHARS):
        raise ValueError(f"Invalid attachment filename: {filename!r}")
    return filename


def generate_yuppaste_link(paste_uuid: str) -> str:
    """Generate go-link for a Yuppaste UUID."""
    return f"http://go/p/{paste_uuid}"


def generate_yuppaste_slug_link(named_slug: str, version: int | None = None) -> str:
    """Generate go-link for a named Yuppaste slug."""
    if version is not None:
        return f"http://go/p/{named_slug}@{version}"
    return f"http://go/p/{named_slug}"


async def get_max_version_for_slug(named_slug: str) -> int | None:
    """Get the maximum version number for a named slug.

    Includes archived versions to prevent version number reuse.

    Args:
        named_slug: The slug to query

    Returns:
        Maximum version number (including archived), or None if slug never existed
    """
    client = get_bigquery_client()
    # Include ALL versions (even archived) to prevent version number reuse
    query = f"""
    SELECT MAX(version) as max_version
    FROM `{settings.GCP_PROJECT_ID}.{settings.YUPPASTE_BQ_DATASET}.{settings.YUPPASTE_BQ_TABLE}`
    WHERE named_slug = @named_slug
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("named_slug", "STRING", named_slug),
        ]
    )

    def _execute_query() -> int | None:
        query_job = client.query(query, job_config=job_config)
        result = query_job.result()
        row = next(result, None)
        if row is None:
            return None
        max_version = row.max_version
        return int(max_version) if max_version is not None else None

    return await asyncio.to_thread(_execute_query)


async def slug_exists(named_slug: str) -> bool:
    """Check if a named slug exists (has any non-archived versions).

    Args:
        named_slug: The slug to check

    Returns:
        True if the slug exists with at least one active version
    """
    client = get_bigquery_client()
    query = f"""
    SELECT 1
    FROM `{settings.GCP_PROJECT_ID}.{settings.YUPPASTE_BQ_DATASET}.{settings.YUPPASTE_BQ_TABLE}`
    WHERE named_slug = @named_slug AND (is_archived IS NULL OR is_archived = FALSE)
    LIMIT 1
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("named_slug", "STRING", named_slug),
        ]
    )

    def _execute_query() -> bool:
        query_job = client.query(query, job_config=job_config)
        result = query_job.result()
        return next(result, None) is not None

    return await asyncio.to_thread(_execute_query)


async def get_pastes_metadata_from_bigquery(
    page: int,
    page_size: int,
    created_by: str | None,
    sort_by: str,
    sort_order: str,
    named_slug: str | None = None,
    include_archived: bool = False,
) -> YuppasteListResponse:
    client = get_bigquery_client()

    # Calculate offset for pagination
    offset = (page - 1) * page_size

    # Build WHERE clauses
    where_clauses = []
    query_parameters = [
        bigquery.ScalarQueryParameter("page_size", "INT64", page_size),
        bigquery.ScalarQueryParameter("offset", "INT64", offset),
    ]

    if created_by:
        where_clauses.append("created_by = @created_by")
        query_parameters.append(bigquery.ScalarQueryParameter("created_by", "STRING", created_by))

    if named_slug:
        where_clauses.append("named_slug = @named_slug")
        query_parameters.append(bigquery.ScalarQueryParameter("named_slug", "STRING", named_slug))

    if not include_archived:
        where_clauses.append("(is_archived IS NULL OR is_archived = FALSE)")

    where_clause = " AND ".join(where_clauses) if where_clauses else "1=1"

    count_query = f"""
    SELECT COUNT(*) as total_count
    FROM `{settings.GCP_PROJECT_ID}.{settings.YUPPASTE_BQ_DATASET}.{settings.YUPPASTE_BQ_TABLE}`
    WHERE {where_clause}
    """

    data_query = f"""
    SELECT uuid, name, created_by, gcs_url, created_at, named_slug, version, is_archived
    FROM `{settings.GCP_PROJECT_ID}.{settings.YUPPASTE_BQ_DATASET}.{settings.YUPPASTE_BQ_TABLE}`
    WHERE {where_clause}
    ORDER BY {sort_by} {sort_order}
    LIMIT @page_size
    OFFSET @offset
    """

    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)

    def _execute_bigquery_queries() -> tuple[int, list]:
        count_job = client.query(count_query, job_config=job_config)
        count_result = count_job.result()
        total_count = next(count_result).total_count

        data_job = client.query(data_query, job_config=job_config)
        data_result = data_job.result()
        return total_count, list(data_result)

    try:
        total_count, data_result = await asyncio.to_thread(_execute_bigquery_queries)
    except google_exceptions.Forbidden as e:
        logging.error(
            json_dumps(
                {"message": "BigQuery permission denied for yuppaste table read", "facilitator": created_by},
            ),
            exc_info=True,
        )
        raise ValueError("BigQuery table access denied. Please check permissions.") from e
    except Exception as e:
        logging.error(
            json_dumps(
                {"message": "BigQuery error during yuppaste metadata retrieval", "facilitator": created_by},
            ),
            exc_info=True,
        )
        raise ValueError("Failed to retrieve yuppaste metadata from BigQuery.") from e

    # Convert results to Pydantic models
    pastes = []
    for row in data_result:
        paste = YuppasteMetadata(
            uuid=str(row.uuid),
            name=str(row.name) if row.name is not None else None,
            created_by=str(row.created_by),
            gcs_url=str(row.gcs_url),
            created_at=row.created_at,
            named_slug=str(row.named_slug) if getattr(row, "named_slug", None) is not None else None,
            version=int(row.version) if getattr(row, "version", None) is not None else None,
            is_archived=bool(row.is_archived) if getattr(row, "is_archived", None) is not None else False,
        )
        pastes.append(paste)

    # Calculate pagination info
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
    # Handle versioning logic
    version: int | None = None
    if named_slug:
        validate_named_slug(named_slug)
        exists = await slug_exists(named_slug)
        if create_new_slug:
            if exists:
                raise ValueError(f"named_slug '{named_slug}' already exists")
            # Check for archived versions to prevent version reuse
            max_version = await get_max_version_for_slug(named_slug)
            version = (max_version or 0) + 1
        else:
            if not exists:
                raise ValueError(f"named_slug '{named_slug}' does not exist. Use create_new_slug=True to create it.")
            max_version = await get_max_version_for_slug(named_slug)
            version = (max_version or 0) + 1

    # Generate UUID and GCS URL
    file_uuid = str(uuid.uuid4())
    gcs_url = f"gs://{settings.GCS_BUCKET_NAME}/pastes/{file_uuid}.txt"
    created_at = datetime.now(UTC)

    # Upload data to GCS using async client
    async with Storage() as async_client:
        await async_client.upload(
            bucket=settings.GCS_BUCKET_NAME,
            object_name=f"pastes/{file_uuid}.txt",
            file_data=data.encode("utf-8"),
            content_type=content_type,
        )

    # Insert metadata into BigQuery
    client = get_bigquery_client()
    query = f"""
    INSERT INTO `{settings.GCP_PROJECT_ID}.{settings.YUPPASTE_BQ_DATASET}.{settings.YUPPASTE_BQ_TABLE}`
    (uuid, name, created_by, gcs_url, created_at, named_slug, version, is_archived)
    VALUES (@uuid, @name, @created_by, @gcs_url, @created_at, @named_slug, @version, @is_archived)
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("uuid", "STRING", file_uuid),
            bigquery.ScalarQueryParameter("name", "STRING", name),
            bigquery.ScalarQueryParameter("created_by", "STRING", created_by),
            bigquery.ScalarQueryParameter("gcs_url", "STRING", gcs_url),
            bigquery.ScalarQueryParameter("created_at", "TIMESTAMP", created_at),
            bigquery.ScalarQueryParameter("named_slug", "STRING", named_slug),
            bigquery.ScalarQueryParameter("version", "INT64", version),
            bigquery.ScalarQueryParameter("is_archived", "BOOL", False),
        ]
    )

    def _execute_bigquery_insert() -> None:
        client.query(query, job_config=job_config).result()

    await asyncio.to_thread(_execute_bigquery_insert)
    return YuppasteCreateResponse(
        uuid=file_uuid,
        name=name,
        gcs_url=gcs_url,
        created_at=created_at,
        named_slug=named_slug,
        version=version,
        is_archived=False,
    )


def _replace_attachment_placeholders(data: str, file_uuid: str, filenames: set[str]) -> str:
    """Replace attachment placeholders with GCS URLs.

    Converts `![alt](attachment:filename)` to `![alt](gs://bucket/pastes/attachments/uuid/filename)`.
    """

    def _replacer(match: re.Match[str]) -> str:
        alt_text = match.group(1)
        filename = match.group(2)
        if filename in filenames:
            gcs_url = f"gs://{settings.GCS_BUCKET_NAME}/pastes/attachments/{file_uuid}/{filename}"
            return f"![{alt_text}]({gcs_url})"
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
    # Handle versioning logic
    version: int | None = None
    if named_slug:
        validate_named_slug(named_slug)
        exists = await slug_exists(named_slug)
        if create_new_slug:
            if exists:
                raise ValueError(f"named_slug '{named_slug}' already exists")
            # Check for archived versions to prevent version reuse
            max_version = await get_max_version_for_slug(named_slug)
            version = (max_version or 0) + 1
        else:
            if not exists:
                raise ValueError(f"named_slug '{named_slug}' does not exist. Use create_new_slug=True to create it.")
            max_version = await get_max_version_for_slug(named_slug)
            version = (max_version or 0) + 1

    attachments = attachments or []

    if len(attachments) > MAX_ATTACHMENTS_PER_PASTE:
        raise ValueError(f"Too many attachments: {len(attachments)} (max {MAX_ATTACHMENTS_PER_PASTE})")

    # Sanitize filenames and validate attachments
    attachments = [(_sanitize_attachment_filename(fn), content, ct) for fn, content, ct in attachments]

    # Reject duplicate filenames — later uploads would silently overwrite earlier ones in GCS
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

    file_uuid = str(uuid.uuid4())
    gcs_url = f"gs://{settings.GCS_BUCKET_NAME}/pastes/{file_uuid}.txt"
    created_at = datetime.now(UTC)

    # Upload attachments concurrently
    attachment_infos: list[AttachmentInfo] = []
    if attachments:
        filenames = {filename for filename, _, _ in attachments}
        # Replace placeholders before uploading text
        data = _replace_attachment_placeholders(data, file_uuid, filenames)

        async def _upload_attachment(filename: str, content: bytes, content_type: str) -> AttachmentInfo:
            object_name = f"pastes/attachments/{file_uuid}/{filename}"
            async with Storage() as client:
                await client.upload(
                    bucket=settings.GCS_BUCKET_NAME,
                    object_name=object_name,
                    file_data=content,
                    content_type=content_type,
                )
            return AttachmentInfo(
                filename=filename,
                gcs_url=f"gs://{settings.GCS_BUCKET_NAME}/{object_name}",
                content_type=content_type,
                size_bytes=len(content),
            )

        attachment_infos = list(
            await asyncio.gather(*[_upload_attachment(fn, content, ctype) for fn, content, ctype in attachments])
        )

    # Upload text content
    async with Storage() as async_client:
        await async_client.upload(
            bucket=settings.GCS_BUCKET_NAME,
            object_name=f"pastes/{file_uuid}.txt",
            file_data=data.encode("utf-8"),
            content_type=content_type,
        )

    # Insert metadata into BigQuery with attachments JSON
    client = get_bigquery_client()
    attachments_json = json.dumps([ai.model_dump() for ai in attachment_infos]) if attachment_infos else None

    query = f"""
    INSERT INTO `{settings.GCP_PROJECT_ID}.{settings.YUPPASTE_BQ_DATASET}.{settings.YUPPASTE_BQ_TABLE}`
    (uuid, name, created_by, gcs_url, created_at, attachments, named_slug, version, is_archived)
    VALUES (@uuid, @name, @created_by, @gcs_url, @created_at,
            PARSE_JSON(@attachments), @named_slug, @version, @is_archived)
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("uuid", "STRING", file_uuid),
            bigquery.ScalarQueryParameter("name", "STRING", name),
            bigquery.ScalarQueryParameter("created_by", "STRING", created_by),
            bigquery.ScalarQueryParameter("gcs_url", "STRING", gcs_url),
            bigquery.ScalarQueryParameter("created_at", "TIMESTAMP", created_at),
            bigquery.ScalarQueryParameter("attachments", "STRING", attachments_json),
            bigquery.ScalarQueryParameter("named_slug", "STRING", named_slug),
            bigquery.ScalarQueryParameter("version", "INT64", version),
            bigquery.ScalarQueryParameter("is_archived", "BOOL", False),
        ]
    )

    def _execute_bigquery_insert() -> None:
        client.query(query, job_config=job_config).result()

    await asyncio.to_thread(_execute_bigquery_insert)

    return YuppasteCreateWithAttachmentsResponse(
        uuid=file_uuid,
        name=name,
        gcs_url=gcs_url,
        created_at=created_at,
        attachments=attachment_infos,
        named_slug=named_slug,
        version=version,
        is_archived=False,
    )


async def get_yuppaste_by_uuid(paste_uuid: str) -> YuppasteContentResponse:
    """Get yuppaste content and metadata by UUID."""
    # Get metadata from BigQuery (including attachments)
    client = get_bigquery_client()
    query = f"""
    SELECT uuid, name, created_by, gcs_url, created_at, TO_JSON_STRING(attachments) as attachments_json,
           named_slug, version, is_archived
    FROM `{settings.GCP_PROJECT_ID}.{settings.YUPPASTE_BQ_DATASET}.{settings.YUPPASTE_BQ_TABLE}`
    WHERE uuid = @uuid
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("uuid", "STRING", paste_uuid),
        ]
    )

    def _execute_bigquery_query() -> list:
        query_job = client.query(query, job_config=job_config)
        result = query_job.result()
        return list(result)

    result = await asyncio.to_thread(_execute_bigquery_query)

    # Check if paste exists
    if not result:
        raise ValueError(f"Yuppaste with UUID {paste_uuid} not found")

    row = result[0]

    # Parse attachments from JSON
    attachment_infos: list[AttachmentInfo] = []
    attachments_json = getattr(row, "attachments_json", None)
    if attachments_json:
        try:
            parsed = json.loads(attachments_json)
            if isinstance(parsed, list):
                attachment_infos = [AttachmentInfo(**item) for item in parsed]
        except (json.JSONDecodeError, TypeError):
            logging.warning(f"Failed to parse attachments JSON for paste {paste_uuid}")

    # Check object size in GCS then download if it's small enough
    try:
        async with Storage() as async_client:
            bucket = async_client.get_bucket(settings.GCS_BUCKET_NAME)
            async with aiohttp.ClientSession() as session:
                blob = await bucket.get_blob(f"pastes/{paste_uuid}.txt", session=session)  # type: ignore[arg-type]

                if blob is None:
                    raise ValueError(f"Yuppaste content not found in GCS for UUID {paste_uuid}")

                if blob.size > MAX_RESPONSE_CONTENT_SIZE_BYTES:
                    # Use the blob's authenticated URL for direct access
                    authenticated_url = (
                        f"https://storage.cloud.google.com/{settings.GCS_BUCKET_NAME}/pastes/{paste_uuid}.txt"
                    )
                    return YuppasteContentResponse(
                        uuid=str(row.uuid),
                        name=str(row.name) if row.name is not None else None,
                        data=None,
                        created_by=str(row.created_by),
                        gcs_url=str(row.gcs_url),
                        redirect_url=authenticated_url,
                        file_size=blob.size,
                        created_at=row.created_at,
                        attachments=attachment_infos,
                        named_slug=str(row.named_slug) if getattr(row, "named_slug", None) is not None else None,
                        version=int(row.version) if getattr(row, "version", None) is not None else None,
                        is_archived=bool(row.is_archived) if getattr(row, "is_archived", None) is not None else False,
                    )

                # Download content for smaller files
                content_bytes = await async_client.download(
                    bucket=settings.GCS_BUCKET_NAME,
                    object_name=f"pastes/{paste_uuid}.txt",
                )
                content = content_bytes.decode("utf-8")
    except Exception as e:
        if "Not Found" in str(e) or "404" in str(e):
            raise ValueError(f"Yuppaste content not found in GCS for UUID {paste_uuid}") from None
        raise

    return YuppasteContentResponse(
        uuid=str(row.uuid),
        name=str(row.name) if row.name is not None else None,
        data=content,
        created_by=str(row.created_by),
        gcs_url=str(row.gcs_url),
        redirect_url=None,
        file_size=blob.size,
        created_at=row.created_at,
        attachments=attachment_infos,
        named_slug=str(row.named_slug) if getattr(row, "named_slug", None) is not None else None,
        version=int(row.version) if getattr(row, "version", None) is not None else None,
        is_archived=bool(row.is_archived) if getattr(row, "is_archived", None) is not None else False,
    )


async def get_yuppaste_by_slug(named_slug: str, version: int | None = None) -> YuppasteContentResponse:
    """Get yuppaste content and metadata by named slug.

    Args:
        named_slug: The slug to look up
        version: Optional specific version. If None, returns the latest non-archived version.

    Returns:
        YuppasteContentResponse with paste content and metadata

    Raises:
        ValueError: If no paste found with the given slug/version
    """
    client = get_bigquery_client()

    if version is not None:
        # Get specific version
        query = f"""
        SELECT uuid, name, created_by, gcs_url, created_at, TO_JSON_STRING(attachments) as attachments_json,
               named_slug, version, is_archived
        FROM `{settings.GCP_PROJECT_ID}.{settings.YUPPASTE_BQ_DATASET}.{settings.YUPPASTE_BQ_TABLE}`
        WHERE named_slug = @named_slug AND version = @version
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("named_slug", "STRING", named_slug),
                bigquery.ScalarQueryParameter("version", "INT64", version),
            ]
        )
    else:
        # Get latest non-archived version
        query = f"""
        SELECT uuid, name, created_by, gcs_url, created_at, TO_JSON_STRING(attachments) as attachments_json,
               named_slug, version, is_archived
        FROM `{settings.GCP_PROJECT_ID}.{settings.YUPPASTE_BQ_DATASET}.{settings.YUPPASTE_BQ_TABLE}`
        WHERE named_slug = @named_slug AND (is_archived IS NULL OR is_archived = FALSE)
        ORDER BY version DESC
        LIMIT 1
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("named_slug", "STRING", named_slug),
            ]
        )

    def _execute_bigquery_query() -> list:
        query_job = client.query(query, job_config=job_config)
        result = query_job.result()
        return list(result)

    result = await asyncio.to_thread(_execute_bigquery_query)

    if not result:
        if version is not None:
            raise ValueError(f"Yuppaste with slug '{named_slug}' version {version} not found")
        raise ValueError(f"Yuppaste with slug '{named_slug}' not found")

    row = result[0]
    paste_uuid = str(row.uuid)

    # Parse attachments from JSON
    attachment_infos: list[AttachmentInfo] = []
    attachments_json = getattr(row, "attachments_json", None)
    if attachments_json:
        try:
            parsed = json.loads(attachments_json)
            if isinstance(parsed, list):
                attachment_infos = [AttachmentInfo(**item) for item in parsed]
        except (json.JSONDecodeError, TypeError):
            logging.warning(f"Failed to parse attachments JSON for paste {paste_uuid}")

    # Check object size in GCS then download if it's small enough
    try:
        async with Storage() as async_client:
            bucket = async_client.get_bucket(settings.GCS_BUCKET_NAME)
            async with aiohttp.ClientSession() as session:
                blob = await bucket.get_blob(f"pastes/{paste_uuid}.txt", session=session)  # type: ignore[arg-type]

                if blob is None:
                    raise ValueError(f"Yuppaste content not found in GCS for slug '{named_slug}'")

                if blob.size > MAX_RESPONSE_CONTENT_SIZE_BYTES:
                    authenticated_url = (
                        f"https://storage.cloud.google.com/{settings.GCS_BUCKET_NAME}/pastes/{paste_uuid}.txt"
                    )
                    return YuppasteContentResponse(
                        uuid=paste_uuid,
                        name=str(row.name) if row.name is not None else None,
                        data=None,
                        created_by=str(row.created_by),
                        gcs_url=str(row.gcs_url),
                        redirect_url=authenticated_url,
                        file_size=blob.size,
                        created_at=row.created_at,
                        attachments=attachment_infos,
                        named_slug=str(row.named_slug) if getattr(row, "named_slug", None) is not None else None,
                        version=int(row.version) if getattr(row, "version", None) is not None else None,
                        is_archived=bool(row.is_archived) if getattr(row, "is_archived", None) is not None else False,
                    )

                content_bytes = await async_client.download(
                    bucket=settings.GCS_BUCKET_NAME,
                    object_name=f"pastes/{paste_uuid}.txt",
                )
                content = content_bytes.decode("utf-8")
    except Exception as e:
        if "Not Found" in str(e) or "404" in str(e):
            raise ValueError(f"Yuppaste content not found in GCS for UUID {paste_uuid}") from None
        raise

    return YuppasteContentResponse(
        uuid=paste_uuid,
        name=str(row.name) if row.name is not None else None,
        data=content,
        created_by=str(row.created_by),
        gcs_url=str(row.gcs_url),
        redirect_url=None,
        file_size=blob.size,
        created_at=row.created_at,
        attachments=attachment_infos,
        named_slug=str(row.named_slug) if getattr(row, "named_slug", None) is not None else None,
        version=int(row.version) if getattr(row, "version", None) is not None else None,
        is_archived=bool(row.is_archived) if getattr(row, "is_archived", None) is not None else False,
    )


async def archive_yuppaste(paste_uuid: str, archived_by: str) -> YuppasteMetadata:
    """Archive a yuppaste by UUID.

    Sets is_archived=True on the paste. Archived pastes:
    - Are excluded from default list queries (unless include_archived=True)
    - Are excluded when resolving a slug without a specific version
    - Can still be accessed directly by UUID or by slug@version
    - Preserve their version number (prevents version reuse)

    Args:
        paste_uuid: UUID of the paste to archive
        archived_by: Email of the user archiving the paste

    Returns:
        Updated metadata of the archived paste (with is_archived=True)

    Raises:
        ValueError: If paste not found
        PermissionError: If user is not the creator
    """
    client = get_bigquery_client()

    # First, get the current paste to check authorization
    select_query = f"""
    SELECT uuid, name, created_by, gcs_url, created_at, named_slug, version, is_archived
    FROM `{settings.GCP_PROJECT_ID}.{settings.YUPPASTE_BQ_DATASET}.{settings.YUPPASTE_BQ_TABLE}`
    WHERE uuid = @uuid
    """

    select_job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("uuid", "STRING", paste_uuid)]
    )

    def _execute_bigquery_select() -> Any:
        select_job = client.query(select_query, job_config=select_job_config)
        select_result = select_job.result()
        return next(select_result, None)

    row = await asyncio.to_thread(_execute_bigquery_select)

    if row is None:
        raise ValueError(f"Yuppaste with UUID {paste_uuid} not found")

    if str(row.created_by) != archived_by:
        raise PermissionError("You can only archive yuppastes created by yourself")

    # Update is_archived to True
    update_query = f"""
    UPDATE `{settings.GCP_PROJECT_ID}.{settings.YUPPASTE_BQ_DATASET}.{settings.YUPPASTE_BQ_TABLE}`
    SET is_archived = TRUE
    WHERE uuid = @uuid
    """

    update_job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("uuid", "STRING", paste_uuid)]
    )

    def _execute_bigquery_update() -> int:
        query_job = client.query(update_query, job_config=update_job_config)
        result = query_job.result()
        return result.num_dml_affected_rows or 0

    await asyncio.to_thread(_execute_bigquery_update)

    return YuppasteMetadata(
        uuid=str(row.uuid),
        name=str(row.name) if row.name is not None else None,
        created_by=str(row.created_by),
        gcs_url=str(row.gcs_url),
        created_at=row.created_at,
        named_slug=str(row.named_slug) if getattr(row, "named_slug", None) is not None else None,
        version=int(row.version) if getattr(row, "version", None) is not None else None,
        is_archived=True,
    )


async def archive_yuppaste_by_slug(named_slug: str, version: int | None, archived_by: str) -> int:
    """Archive yuppaste(s) by named slug.

    Args:
        named_slug: The slug to archive
        version: Specific version to archive. If None, archives all versions (no ownership check).
        archived_by: Email of the user archiving

    Returns:
        Number of pastes archived

    Raises:
        PermissionError: If archiving a specific version not owned by archived_by
    """
    client = get_bigquery_client()

    # Verify ownership only for single-version archive.
    # Bulk archive (version=None) trusts slug-level access - anyone who knows
    # the slug can archive all versions, supporting collaborative slug workflows.
    if version is not None:
        check_query = f"""
        SELECT created_by
        FROM `{settings.GCP_PROJECT_ID}.{settings.YUPPASTE_BQ_DATASET}.{settings.YUPPASTE_BQ_TABLE}`
        WHERE named_slug = @named_slug AND version = @version
        """
        check_params = [
            bigquery.ScalarQueryParameter("named_slug", "STRING", named_slug),
            bigquery.ScalarQueryParameter("version", "INT64", version),
        ]
        check_job_config = bigquery.QueryJobConfig(query_parameters=check_params)

        def _check_ownership() -> list[str]:
            query_job = client.query(check_query, job_config=check_job_config)
            result = query_job.result()
            return [str(row.created_by) for row in result]

        creators = await asyncio.to_thread(_check_ownership)

        if not creators:
            return 0

        for creator in creators:
            if creator != archived_by:
                raise PermissionError("You can only archive yuppastes created by yourself")

    # Archive
    if version is not None:
        update_query = f"""
        UPDATE `{settings.GCP_PROJECT_ID}.{settings.YUPPASTE_BQ_DATASET}.{settings.YUPPASTE_BQ_TABLE}`
        SET is_archived = TRUE
        WHERE named_slug = @named_slug AND version = @version
        """
        update_params = [
            bigquery.ScalarQueryParameter("named_slug", "STRING", named_slug),
            bigquery.ScalarQueryParameter("version", "INT64", version),
        ]
    else:
        update_query = f"""
        UPDATE `{settings.GCP_PROJECT_ID}.{settings.YUPPASTE_BQ_DATASET}.{settings.YUPPASTE_BQ_TABLE}`
        SET is_archived = TRUE
        WHERE named_slug = @named_slug AND (is_archived IS NULL OR is_archived = FALSE)
        """
        update_params = [
            bigquery.ScalarQueryParameter("named_slug", "STRING", named_slug),
        ]

    update_job_config = bigquery.QueryJobConfig(query_parameters=update_params)

    def _execute_archive() -> int:
        query_job = client.query(update_query, job_config=update_job_config)
        result = query_job.result()
        return result.num_dml_affected_rows or 0

    return await asyncio.to_thread(_execute_archive)


async def update_yuppaste_metadata(
    paste_uuid: str, name: str | None = None, update_by: str | None = None
) -> YuppasteMetadata:
    """Update yuppaste metadata."""
    client = get_bigquery_client()

    # First, get the current paste to check authorization
    select_query = f"""
    SELECT uuid, name, created_by, gcs_url, created_at, named_slug, version, is_archived
    FROM `{settings.GCP_PROJECT_ID}.{settings.YUPPASTE_BQ_DATASET}.{settings.YUPPASTE_BQ_TABLE}`
    WHERE uuid = @uuid
    """

    select_job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("uuid", "STRING", paste_uuid)]
    )

    def _execute_bigquery_select() -> Any:
        select_job = client.query(select_query, job_config=select_job_config)
        select_result = select_job.result()
        return next(select_result)

    row = await asyncio.to_thread(_execute_bigquery_select)

    # Check authorization if created_by is provided
    if update_by is not None and str(row.created_by) != update_by:
        raise PermissionError("You can only update yuppastes created by yourself")

    # Build update query based on provided fields
    update_fields = []
    query_parameters = [bigquery.ScalarQueryParameter("uuid", "STRING", paste_uuid)]

    if name is not None:
        update_fields.append("name = @name")
        query_parameters.append(bigquery.ScalarQueryParameter("name", "STRING", name))

    if not update_fields:
        raise ValueError("No fields provided for update")

    query = f"""
    UPDATE `{settings.GCP_PROJECT_ID}.{settings.YUPPASTE_BQ_DATASET}.{settings.YUPPASTE_BQ_TABLE}`
    SET {", ".join(update_fields)}
    WHERE uuid = @uuid
    """

    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)

    def _execute_bigquery_update() -> int:
        query_job = client.query(query, job_config=job_config)
        result = query_job.result()
        return result.num_dml_affected_rows or 0

    num_affected_rows = await asyncio.to_thread(_execute_bigquery_update)

    # Check if any rows were affected
    if num_affected_rows is None or num_affected_rows == 0:
        raise ValueError(f"Yuppaste with UUID {paste_uuid} not found")

    # Get updated metadata
    updated_row = await asyncio.to_thread(_execute_bigquery_select)

    return YuppasteMetadata(
        uuid=str(updated_row.uuid),
        name=str(updated_row.name) if updated_row.name is not None else None,
        created_by=str(updated_row.created_by),
        gcs_url=str(updated_row.gcs_url),
        created_at=updated_row.created_at,
        named_slug=str(updated_row.named_slug) if getattr(updated_row, "named_slug", None) is not None else None,
        version=int(updated_row.version) if getattr(updated_row, "version", None) is not None else None,
        is_archived=bool(updated_row.is_archived) if getattr(updated_row, "is_archived", None) is not None else False,
    )
