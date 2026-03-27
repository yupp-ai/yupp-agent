import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

# URL-safe slug pattern: 1-63 chars, starts with alphanumeric, contains only alphanumeric/hyphens/underscores
NAMED_SLUG_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")


def validate_named_slug(slug: str) -> None:
    """Validate that a slug matches the required pattern.

    Args:
        slug: The slug to validate

    Raises:
        ValueError: If the slug is invalid
    """
    if not NAMED_SLUG_PATTERN.match(slug):
        raise ValueError(
            "named_slug must be 1-63 chars, start with alphanumeric, "
            "contain only alphanumeric characters, hyphens, or underscores"
        )


class AttachmentInfo(BaseModel):
    """Metadata for a single attachment in a yuppaste."""

    filename: str = Field(..., description="Original filename of the attachment")
    gcs_url: str = Field(..., description="GCS URL where the attachment is stored")
    content_type: str = Field(..., description="MIME content type of the attachment")
    size_bytes: int = Field(..., description="Size of the attachment in bytes")


class YuppasteMetadata(BaseModel):
    """Metadata for a yuppaste entry."""

    uuid: str = Field(..., description="Unique identifier for the paste")
    name: str | None = Field(None, description="Name/title of the paste")
    created_by: str = Field(..., description="Email of the user who created the paste")
    gcs_url: str = Field(..., description="Google Cloud Storage URL for the paste content")
    created_at: datetime = Field(..., description="Timestamp when the paste was created")
    named_slug: str | None = Field(None, description="URL-safe lookup key for named pastes")
    version: int | None = Field(None, description="Version number for named pastes")
    is_archived: bool = Field(False, description="Whether the paste is archived")


class YuppasteListResponse(BaseModel):
    """Response model for the yuppaste list endpoint."""

    pastes: list[YuppasteMetadata] = Field(..., description="List of paste metadata")
    total_count: int = Field(..., description="Total number of pastes available")
    page: int = Field(..., description="Current page number")
    page_size: int = Field(..., description="Number of items per page")
    has_next: bool = Field(..., description="Whether there are more pages available")
    has_previous: bool = Field(..., description="Whether there are previous pages available")


class YuppasteCreateRequest(BaseModel):
    """Request model for creating a new yuppaste."""

    data: str = Field(..., description="Content data to be stored in the yuppaste")
    name: str | None = Field(None, description="Name/title of the paste")
    content_type: str = Field("text/plain", description="MIME type for the content")
    named_slug: str | None = Field(
        None, description="URL-safe lookup key (1-63 chars, alphanumeric/hyphens/underscores)"
    )
    create_new_slug: bool = Field(
        False, description="True = create new slug (version 1), False = add version to existing slug"
    )

    @field_validator("named_slug")
    @classmethod
    def validate_slug(cls, v: str | None) -> str | None:
        if v is not None:
            validate_named_slug(v)
        return v

    @model_validator(mode="after")
    def check_slug_fields(self) -> "YuppasteCreateRequest":
        if self.create_new_slug and not self.named_slug:
            raise ValueError("named_slug is required when create_new_slug is True")
        return self


class YuppasteCreateResponse(BaseModel):
    """Response model for creating a new yuppaste."""

    uuid: str = Field(..., description="Unique identifier for the created paste")
    name: str | None = Field(None, description="Name/title of the paste")
    gcs_url: str = Field(..., description="Google Cloud Storage URL for the paste content")
    created_at: datetime = Field(..., description="Timestamp when the paste was created")
    named_slug: str | None = Field(None, description="URL-safe lookup key for named pastes")
    version: int | None = Field(None, description="Version number for named pastes")
    is_archived: bool = Field(False, description="Whether the paste is archived")


class YuppasteCreateWithAttachmentsResponse(BaseModel):
    """Response model for creating a yuppaste with attachments."""

    uuid: str = Field(..., description="Unique identifier for the created paste")
    name: str | None = Field(None, description="Name/title of the paste")
    gcs_url: str = Field(..., description="Google Cloud Storage URL for the paste content")
    created_at: datetime = Field(..., description="Timestamp when the paste was created")
    attachments: list[AttachmentInfo] = Field(default_factory=list, description="List of uploaded attachments")
    named_slug: str | None = Field(None, description="URL-safe lookup key for named pastes")
    version: int | None = Field(None, description="Version number for named pastes")
    is_archived: bool = Field(False, description="Whether the paste is archived")


class YuppasteContentResponse(BaseModel):
    """Response model for getting yuppaste content."""

    uuid: str = Field(..., description="Unique identifier for the paste")
    name: str | None = Field(None, description="Name/title of the paste")
    data: str | None = Field(None, description="Content data of the paste (None if file is too large)")
    created_by: str = Field(..., description="Email of the user who created the paste")
    gcs_url: str = Field(..., description="Google Cloud Storage URL for the paste content")
    redirect_url: str | None = Field(None, description="Authenticated URL for direct access to large files")
    file_size: int | None = Field(None, description="Size of the file in bytes")
    created_at: datetime = Field(..., description="Timestamp when the paste was created")
    attachments: list[AttachmentInfo] = Field(default_factory=list, description="List of attachments")
    named_slug: str | None = Field(None, description="URL-safe lookup key for named pastes")
    version: int | None = Field(None, description="Version number for named pastes")
    is_archived: bool = Field(False, description="Whether the paste is archived")


class YuppasteUpdateRequest(BaseModel):
    """Request model for updating yuppaste metadata."""

    name: str | None = Field(None, description="Name/title of the paste")
