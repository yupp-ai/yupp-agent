"""MCP tools for yuppaste creation and retrieval.

Provides tools to create and read yuppastes (shareable text snippets).
"""

import json
from typing import Any

from ypl.agent_harness_service.common.signing import sign_artifact_url
from ypl.backend.internal_tools.yuppaste_backend import (
    create_yuppaste,
    create_yuppaste_with_attachments,
    generate_yuppaste_link,
    generate_yuppaste_slug_link,
    get_yuppaste_by_slug,
    get_yuppaste_by_uuid,
)
from ypl.mcp_common.scheduled_agent_call_helpers import resolve_email_from_user_id
from ypl.mcp_server.core import get_authenticated_user_email, get_requesting_user_id, mcp_server
from ypl.structured_logger import get_logger

logger = get_logger()

# Maximum content size for yuppaste uploads (10MB)
_MAX_YUPPASTE_CONTENT_SIZE_BYTES = 10 * 1024 * 1024


def _try_sign_url(uuid: str) -> str | None:
    """Return a signed viewer URL for *uuid*, or ``None`` if signing is not configured."""
    try:
        return sign_artifact_url(uuid)
    except ValueError:
        # ARTIFACT_SIGNING_SECRET not configured — signed URLs are optional.
        return None


@mcp_server.tool(
    name="create_yuppaste",
    description=(
        "Create a new yuppaste with the given content. Returns a go-link URL (http://go/p/<uuid>) "
        "that can be shared. Use this to share logs, code snippets, or any text content. "
        "Optionally attach images by passing a JSON string in `attachments` with base64-encoded files: "
        '[{"filename": "screenshot.png", "content_base64": "...", "content_type": "image/png"}]. '
        "Use ![alt](attachment:<filename>) placeholders in content to reference attachments. "
        "For named pastes with versioning: set `named_slug` to a URL-safe identifier (1-63 chars), "
        "and `create_new_slug=True` to create a new slug (version 1) or `False` to add a new version."
    ),
)
async def mcp_create_yuppaste(
    content: str,
    name: str | None = None,
    attachments: str | None = None,
    content_type: str = "text/plain",
    named_slug: str | None = None,
    create_new_slug: bool = False,
) -> dict[str, Any]:
    """Create a new yuppaste with the given content and optional attachments.

    Args:
        content: The text content to store in the paste
        name: Optional name/title for the paste
        attachments: Optional JSON string of base64-encoded attachments.
            Schema: [{"filename": "img.png", "content_base64": "...", "content_type": "image/png"}]
        content_type: MIME type for the main content (default: text/plain)
        named_slug: Optional URL-safe slug for named pastes (1-63 chars, alphanumeric/hyphens/underscores)
        create_new_slug: True to create a new slug (version 1), False to add version to existing slug

    Returns:
        Dictionary containing the paste UUID, shareable go-link URL, and (when signing is
        configured) a ``signed_url`` path of the form ``/p/{uuid}?sig=...&exp=...``.
    """
    import base64
    import binascii

    content_size = len(content.encode("utf-8"))
    if content_size > _MAX_YUPPASTE_CONTENT_SIZE_BYTES:
        max_size_mb = _MAX_YUPPASTE_CONTENT_SIZE_BYTES // (1024 * 1024)
        return {
            "success": False,
            "error": f"Content size exceeds maximum allowed size of {max_size_mb}MB",
        }

    # Prefer X-User-ID header (set by AHS), fall back to authenticated token email.
    created_by: str | None = None
    requesting_user_id = get_requesting_user_id()
    if requesting_user_id:
        created_by = await resolve_email_from_user_id(requesting_user_id)
    if not created_by:
        created_by = get_authenticated_user_email()
    if created_by == "unknown":
        return {"success": False, "error": "Authentication required to create yuppaste"}

    # Validate create_new_slug requires named_slug
    if create_new_slug and not named_slug:
        return {"success": False, "error": "create_new_slug requires named_slug to be specified"}

    try:
        # Parse attachments if provided
        attachment_tuples: list[tuple[str, bytes, str]] = []
        if attachments:
            try:
                parsed_attachments = json.loads(attachments)
                if not isinstance(parsed_attachments, list):
                    return {"success": False, "error": "Attachments must be a JSON array"}
                for item in parsed_attachments:
                    filename = item.get("filename")
                    content_b64 = item.get("content_base64")
                    att_content_type = item.get("content_type", "image/png")
                    if not filename or not content_b64:
                        return {
                            "success": False,
                            "error": "Each attachment must have 'filename' and 'content_base64'",
                        }
                    try:
                        decoded = base64.b64decode(content_b64, validate=True)
                    except binascii.Error as e:
                        return {"success": False, "error": f"Invalid base64 for attachment '{filename}': {e}"}
                    attachment_tuples.append((filename, decoded, att_content_type))
            except (json.JSONDecodeError, TypeError) as e:
                return {"success": False, "error": f"Invalid attachments JSON: {e}"}
            except Exception as e:
                return {"success": False, "error": f"Failed to decode attachment: {e}"}

        logger.info(
            "Creating yuppaste",
            name=name,
            content_length=content_size,
            created_by=created_by,
            attachment_count=len(attachment_tuples),
        )

        if attachment_tuples:
            result = await create_yuppaste_with_attachments(
                data=content,
                created_by=created_by,
                name=name,
                attachments=attachment_tuples,
                content_type=content_type,
                named_slug=named_slug,
                create_new_slug=create_new_slug,
            )
            go_link = (
                generate_yuppaste_slug_link(result.named_slug, result.version)
                if result.named_slug
                else generate_yuppaste_link(result.uuid)
            )
            logger.info("Yuppaste created with attachments", uuid=result.uuid, go_link=go_link)
            response: dict[str, Any] = {
                "success": True,
                "uuid": result.uuid,
                "name": result.name,
                "go_link": go_link,
                "gcs_url": result.gcs_url,
                "created_at": result.created_at.isoformat(),
                "attachments": [ai.model_dump() for ai in result.attachments],
            }
            if result.named_slug:
                response["named_slug"] = result.named_slug
                response["version"] = result.version
            signed_url = _try_sign_url(result.uuid)
            if signed_url is not None:
                response["signed_url"] = signed_url
            return response

        result_simple = await create_yuppaste(
            data=content,
            created_by=created_by,
            name=name,
            content_type=content_type,
            named_slug=named_slug,
            create_new_slug=create_new_slug,
        )
        go_link = (
            generate_yuppaste_slug_link(result_simple.named_slug, result_simple.version)
            if result_simple.named_slug
            else generate_yuppaste_link(result_simple.uuid)
        )
        logger.info("Yuppaste created", uuid=result_simple.uuid, go_link=go_link)
        response = {
            "success": True,
            "uuid": result_simple.uuid,
            "name": result_simple.name,
            "go_link": go_link,
            "gcs_url": result_simple.gcs_url,
            "created_at": result_simple.created_at.isoformat(),
        }
        if result_simple.named_slug:
            response["named_slug"] = result_simple.named_slug
            response["version"] = result_simple.version
        signed_url = _try_sign_url(result_simple.uuid)
        if signed_url is not None:
            response["signed_url"] = signed_url
        return response

    except ValueError as e:
        logger.warning("Validation error creating yuppaste", error=str(e))
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning("Error creating yuppaste", error=str(e), exc_info=True)
        return {"success": False, "error": "An unexpected error occurred while creating the yuppaste."}


@mcp_server.tool(
    name="read_yuppaste",
    description=(
        "Read the content of a yuppaste given its UUID or named slug. Use this to retrieve logs, code snippets, "
        "or any text content previously shared via yuppaste. You can extract the UUID from URLs like: "
        "http://go/p/<uuid> or https://yupp-soul.vercel.app/yuppastes/<uuid>. "
        "For named pastes, use the slug directly (e.g., 'my-paste') and optionally specify a version."
    ),
)
async def mcp_read_yuppaste(
    paste_uuid: str | None = None,
    named_slug: str | None = None,
    version: int | None = None,
) -> dict[str, Any]:
    """Read the content of a yuppaste by UUID or named slug.

    Args:
        paste_uuid: The UUID of the yuppaste to read (mutually exclusive with named_slug)
        named_slug: The named slug of the yuppaste to read (mutually exclusive with paste_uuid)
        version: Specific version to read (only applies when using named_slug; if None, returns latest)

    Returns:
        Dictionary containing the paste content, metadata, and go-link URL
    """
    try:
        # Validate that exactly one of paste_uuid or named_slug is provided
        if paste_uuid and named_slug:
            return {"success": False, "error": "Provide either paste_uuid or named_slug, not both"}
        if not paste_uuid and not named_slug:
            return {"success": False, "error": "Must provide either paste_uuid or named_slug"}

        if named_slug:
            logger.info("Reading yuppaste by slug", named_slug=named_slug, version=version)
            result = await get_yuppaste_by_slug(named_slug, version)
        else:
            logger.info("Reading yuppaste", paste_uuid=paste_uuid)
            result = await get_yuppaste_by_uuid(paste_uuid)  # type: ignore[arg-type]

        go_link = (
            generate_yuppaste_slug_link(result.named_slug, result.version)
            if result.named_slug
            else generate_yuppaste_link(result.uuid)
        )

        response: dict[str, Any] = {
            "success": True,
            "uuid": result.uuid,
            "name": result.name,
            "go_link": go_link,
            "created_by": result.created_by,
            "gcs_url": result.gcs_url,
            "created_at": result.created_at.isoformat() if result.created_at else None,
        }

        if result.named_slug:
            response["named_slug"] = result.named_slug
            response["version"] = result.version

        if result.data is not None:
            response["content"] = result.data
        elif result.redirect_url is not None:
            response["content"] = None
            response["redirect_url"] = result.redirect_url
            response["message"] = "Content too large to return inline. Use the redirect URL to access it."

        if result.file_size is not None:
            response["file_size_bytes"] = result.file_size

        if result.attachments:
            response["attachments"] = [ai.model_dump() for ai in result.attachments]

        logger.info("Yuppaste read", uuid=result.uuid, go_link=go_link)

        return response

    except ValueError as e:
        logger.warning("Yuppaste not found", paste_uuid=paste_uuid, named_slug=named_slug, error=str(e))
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning(
            "Error reading yuppaste", paste_uuid=paste_uuid, named_slug=named_slug, error=str(e), exc_info=True
        )
        return {"success": False, "error": "An unexpected error occurred while reading the yuppaste."}
