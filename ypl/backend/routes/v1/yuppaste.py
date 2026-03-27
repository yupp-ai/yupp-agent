from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from ypl.backend.internal_tools.yuppaste_backend import (
    ALLOWED_IMAGE_CONTENT_TYPES,
    MAX_ATTACHMENT_SIZE_BYTES,
    MAX_ATTACHMENTS_PER_PASTE,
    archive_yuppaste,
    archive_yuppaste_by_slug,
    create_yuppaste,
    create_yuppaste_with_attachments,
    get_pastes_metadata_from_bigquery,
    get_yuppaste_by_slug,
    get_yuppaste_by_uuid,
    update_yuppaste_metadata,
)
from ypl.backend.internal_tools.yuppaste_types import (
    YuppasteContentResponse,
    YuppasteCreateRequest,
    YuppasteCreateResponse,
    YuppasteCreateWithAttachmentsResponse,
    YuppasteListResponse,
    YuppasteMetadata,
    YuppasteUpdateRequest,
    validate_named_slug,
)
from ypl.backend.utils.soul_utils import validate_read_yuppaste, validate_write_yuppaste
from ypl.structured_logger import get_logger

router = APIRouter()
logger = get_logger()


_OPTIONAL_FILE_UPLOADS = File(None)


@router.get("/yuppastes", response_model=YuppasteListResponse, dependencies=[Depends(validate_read_yuppaste)])
async def get_yuppaste_metadata(
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(default=50, ge=1, le=100, description="Number of items per page"),
    created_by: str | None = Query(default=None, description="Filter by creator email"),
    sort_by: str = Query(
        default="created_at",
        pattern=r"^(created_at|created_by|name)$",
        description="Sort by field (created_at, created_by, name)",
    ),
    sort_order: str = Query(default="desc", pattern=r"^(asc|desc)$", description="Sort order (asc, desc)"),
    named_slug: str | None = Query(default=None, description="Filter by named slug"),
    include_archived: bool = Query(default=False, description="Include archived pastes"),
) -> YuppasteListResponse:
    """
    Get metadata for yuppaste entries with pagination.

    Returns metadata for pastes stored in BigQuery table yupp_pastes.pastes.
    """
    logger.info("Processing yuppaste metadata request", page=page, page_size=page_size)
    return await get_pastes_metadata_from_bigquery(
        page, page_size, created_by, sort_by, sort_order, named_slug, include_archived
    )


@router.post("/yuppaste", response_model=YuppasteCreateResponse, dependencies=[Depends(validate_write_yuppaste)])
async def create_yuppaste_endpoint(
    request: YuppasteCreateRequest,
    x_creator_email: str = Header(..., alias="X-Creator-Email"),
) -> YuppasteCreateResponse:
    """
    Create a new yuppaste with the provided data.

    Stores the data in Google Cloud Storage and metadata in BigQuery.
    The creator email is taken from the X-Creator-Email header.

    For named pastes with versioning:
    - Set `named_slug` to a URL-safe identifier (1-63 chars, alphanumeric/hyphens/underscores)
    - Set `create_new_slug=True` to create a new slug (version 1)
    - Set `create_new_slug=False` (default) to add a new version to an existing slug
    """
    logger.info("Creating yuppaste", x_creator_email=x_creator_email)
    try:
        return await create_yuppaste(
            data=request.data,
            created_by=x_creator_email,
            name=request.name,
            content_type=request.content_type,
            named_slug=request.named_slug,
            create_new_slug=request.create_new_slug,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post(
    "/yuppaste/with-attachments",
    response_model=YuppasteCreateWithAttachmentsResponse,
    dependencies=[Depends(validate_write_yuppaste)],
)
async def create_yuppaste_with_attachments_endpoint(
    x_creator_email: str = Header(..., alias="X-Creator-Email"),
    name: str | None = Form(None),
    data: str = Form(...),
    content_type: str = Form("text/plain"),
    attachments: list[UploadFile] | None = _OPTIONAL_FILE_UPLOADS,
    named_slug: str | None = Form(None),
    create_new_slug: bool = Form(False),
) -> YuppasteCreateWithAttachmentsResponse:
    """Create a new yuppaste with optional image attachments.

    Accepts multipart/form-data with text content and image files.
    Use `![alt](attachment:<filename>)` in the data to reference attachments.

    For named pastes with versioning:
    - Set `named_slug` to a URL-safe identifier (1-63 chars, alphanumeric/hyphens/underscores)
    - Set `create_new_slug=True` to create a new slug (version 1)
    - Set `create_new_slug=False` (default) to add a new version to an existing slug
    """
    # Validate slug fields (Form params bypass Pydantic model validation)
    if named_slug is not None:
        try:
            validate_named_slug(named_slug)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    if create_new_slug and not named_slug:
        raise HTTPException(status_code=400, detail="named_slug is required when create_new_slug is True")

    attachments = attachments or []
    logger.info(
        "Creating yuppaste with attachments for user",
        attachments_count=len(attachments),
        x_creator_email=x_creator_email,
    )

    if len(attachments) > MAX_ATTACHMENTS_PER_PASTE:
        raise HTTPException(
            status_code=400,
            detail=f"Too many attachments: {len(attachments)} (max {MAX_ATTACHMENTS_PER_PASTE})",
        )

    attachment_tuples: list[tuple[str, bytes, str]] = []
    total_size = 0
    for upload_file in attachments:
        att_content_type = upload_file.content_type or "application/octet-stream"
        if att_content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported content type for '{upload_file.filename}': {att_content_type}. "
                f"Allowed: {', '.join(sorted(ALLOWED_IMAGE_CONTENT_TYPES))}",
            )

        content = await upload_file.read()
        if len(content) > MAX_ATTACHMENT_SIZE_BYTES:
            max_mb = MAX_ATTACHMENT_SIZE_BYTES // (1024 * 1024)
            raise HTTPException(
                status_code=400,
                detail=f"Attachment '{upload_file.filename}' exceeds maximum size of {max_mb}MB",
            )

        total_size += len(content)
        if total_size > MAX_ATTACHMENT_SIZE_BYTES * MAX_ATTACHMENTS_PER_PASTE:
            raise HTTPException(status_code=400, detail="Total attachment size exceeds limit")

        attachment_tuples.append((upload_file.filename or "unnamed", content, att_content_type))

    try:
        return await create_yuppaste_with_attachments(
            data=data,
            created_by=x_creator_email,
            name=name,
            attachments=attachment_tuples,
            content_type=content_type,
            named_slug=named_slug,
            create_new_slug=create_new_slug,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get(
    "/yuppaste/{paste_uuid}", response_model=YuppasteContentResponse, dependencies=[Depends(validate_read_yuppaste)]
)
async def get_yuppaste_by_uuid_endpoint(paste_uuid: str) -> YuppasteContentResponse:
    """
    Get yuppaste content and metadata by UUID.

    Retrieves both the content from Google Cloud Storage and metadata from BigQuery.
    For large files (>1MB), returns a redirect URL instead of content.
    """
    logger.info("Retrieving yuppaste by UUID", paste_uuid=paste_uuid)
    try:
        return await get_yuppaste_by_uuid(paste_uuid)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.put("/yuppaste/{paste_uuid}", response_model=YuppasteMetadata, dependencies=[Depends(validate_write_yuppaste)])
async def update_yuppaste_metadata_endpoint(
    paste_uuid: str,
    request: YuppasteUpdateRequest,
    x_creator_email: str = Header(..., alias="X-Creator-Email"),
) -> YuppasteMetadata:
    """
    Update yuppaste metadata.

    Updates the metadata fields in BigQuery. Only provided fields will be updated.
    Currently supports updating the name field only.
    Only the creator of the yuppaste can update it.
    """
    logger.info("Updating yuppaste metadata", paste_uuid=paste_uuid, x_creator_email=x_creator_email)
    try:
        return await update_yuppaste_metadata(paste_uuid, request.name, x_creator_email)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get(
    "/yuppaste/named/{named_slug}",
    response_model=YuppasteContentResponse,
    dependencies=[Depends(validate_read_yuppaste)],
)
async def get_yuppaste_by_slug_endpoint(
    named_slug: str,
    version: int | None = Query(
        None, ge=1, description="Specific version to retrieve. If not provided, returns latest."
    ),
) -> YuppasteContentResponse:
    """
    Get yuppaste content and metadata by named slug.

    Retrieves both the content from Google Cloud Storage and metadata from BigQuery.
    If version is not specified, returns the latest non-archived version.
    For large files (>1MB), returns a redirect URL instead of content.
    """
    try:
        validate_named_slug(named_slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    logger.info("Retrieving yuppaste by slug", named_slug=named_slug, version=version)
    try:
        return await get_yuppaste_by_slug(named_slug, version)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.delete(
    "/yuppaste/{paste_uuid}",
    response_model=YuppasteMetadata,
    dependencies=[Depends(validate_write_yuppaste)],
)
async def archive_yuppaste_endpoint(
    paste_uuid: str,
    x_creator_email: str = Header(..., alias="X-Creator-Email"),
) -> YuppasteMetadata:
    """
    Archive a yuppaste by UUID.

    Sets the is_archived flag to True. Only the creator can archive a paste.
    Returns the updated metadata of the archived paste.
    """
    logger.info("Archiving yuppaste by UUID", paste_uuid=paste_uuid, x_creator_email=x_creator_email)
    try:
        return await archive_yuppaste(paste_uuid, x_creator_email)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


class YuppasteArchiveBySlugResponse(BaseModel):
    """Response model for archiving yuppastes by slug."""

    archived_count: int = Field(..., description="Number of pastes archived")


@router.delete(
    "/yuppaste/named/{named_slug}",
    response_model=YuppasteArchiveBySlugResponse,
    dependencies=[Depends(validate_write_yuppaste)],
)
async def archive_yuppaste_by_slug_endpoint(
    named_slug: str,
    version: int | None = Query(None, ge=1, description="Specific version to archive. If not provided, archives all."),
    x_creator_email: str = Header(..., alias="X-Creator-Email"),
) -> YuppasteArchiveBySlugResponse:
    """
    Archive yuppaste(s) by named slug.

    If version is specified, archives only that version.
    If version is not specified, archives all non-archived versions of the slug.
    Only the creator can archive pastes. Returns the count of archived pastes.
    Returns 404 if the slug does not exist or has no versions to archive.
    """
    try:
        validate_named_slug(named_slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    logger.info(
        "Archiving yuppaste by slug",
        named_slug=named_slug,
        version=version,
        x_creator_email=x_creator_email,
    )
    try:
        count = await archive_yuppaste_by_slug(named_slug, version, x_creator_email)
        if count == 0:
            raise HTTPException(
                status_code=404,
                detail=f"No yuppaste found with slug '{named_slug}'" + (f" version {version}" if version else ""),
            )
        return YuppasteArchiveBySlugResponse(archived_count=count)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
