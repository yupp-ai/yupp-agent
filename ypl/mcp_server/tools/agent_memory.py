"""MCP tools for agent memory management.

Provides tools for reading, writing, and searching shared agent memory
stored as topic-based markdown files in GCS with optimistic locking.
"""

import re
from datetime import UTC, datetime
from typing import Any

import aiohttp
from gcloud.aio.storage import Storage

from ypl.backend.config import settings
from ypl.backend.utils.gcs_utils import retry_gcs
from ypl.mcp_server.core import mcp_server
from ypl.structured_logger import get_logger

logger = get_logger()

# Agent memory: topic-based files in GCS with optimistic locking.
_AGENT_MEMORY_PREFIX = "memory/"
_AGENT_MEMORY_MAX_TOPIC_SIZE_BYTES = 32 * 1024  # 32KB per topic


class GCSPreconditionFailed(Exception):
    """Raised when GCS returns 412 Precondition Failed (optimistic lock conflict).

    This exception is NOT in retry_gcs's retry list, so it won't be retried.
    The caller should re-read, merge changes, and retry manually.
    """


# ============================================================================
# Helpers
# ============================================================================


def _validate_memory_topic(topic: str) -> str | None:
    """Validate topic name. Returns error message if invalid, None if valid.

    Allows nested folders using '/' separator (e.g., 'bookkeeper/routing-overview').
    Each path segment must start with a letter/number and contain only alphanumeric,
    hyphens, or underscores.
    """
    if not topic:
        return "Topic name cannot be empty"

    if len(topic) > 100:
        return "Topic name must be 100 characters or less"

    # Split into segments and validate each one
    segments = topic.split("/")
    if any(not s for s in segments):
        # Catches leading/trailing/double slashes
        return "Topic cannot have empty segments (no leading, trailing, or double slashes)"

    for segment in segments:
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", segment):
            return (
                f"Invalid segment '{segment}': must start with a letter/number "
                "and contain only alphanumeric, hyphens, or underscores"
            )

    return None


def _sanitize_metadata_value(value: str) -> str:
    """Sanitize a metadata value for safe use in YAML front-matter and HTML comments."""
    # Strip characters that could break YAML structure or HTML comments
    return "".join(c for c in value if c.isalnum() or c in "-_. @/")


def _build_attribution_header(agent_name: str | None, session_id: str | None) -> str:
    """Build a YAML front-matter header with attribution metadata."""
    now = datetime.now(UTC)
    lines = ["---"]
    if agent_name:
        safe_name = _sanitize_metadata_value(agent_name)
        lines.append(f'agent_name: "{safe_name}"')
    if session_id:
        safe_id = _sanitize_metadata_value(session_id)
        lines.append(f'session_id: "{safe_id}"')
    lines.append(f"created_at: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    lines.append("---")
    return "\n".join(lines)


def _build_attribution_marker(agent_name: str | None, session_id: str | None) -> str:
    """Build an inline attribution marker for appended content."""
    now = datetime.now(UTC)
    parts = [f"<!-- updated: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"]
    if agent_name:
        parts.append(f"agent: {_sanitize_metadata_value(agent_name)}")
    if session_id:
        parts.append(f"session: {_sanitize_metadata_value(session_id)}")
    return " | ".join(parts) + " -->"


@retry_gcs
async def _upload_agent_memory(
    bucket: str,
    blob_path: str,
    content_bytes: bytes,
    expected_generation: int,
) -> dict[str, Any]:
    """Upload agent memory to GCS with retry on transient failures.

    Uses retry_gcs decorator for automatic retries on transient GCS errors
    (timeouts, connection errors, 5xx responses).

    Raises:
        GCSPreconditionFailed: If the upload fails due to 412 Precondition Failed
            (optimistic lock conflict). This is NOT retried.
    """
    try:
        async with Storage() as client:
            return await client.upload(
                bucket,
                blob_path,
                content_bytes,
                content_type="text/markdown",
                parameters={"ifGenerationMatch": str(expected_generation)},
            )
    except aiohttp.ClientResponseError as e:
        if e.status == 412:
            # Optimistic lock conflict — don't retry, let caller handle it
            raise GCSPreconditionFailed(str(e)) from e
        raise


# ============================================================================
# MCP Tool Functions
# ============================================================================


@mcp_server.tool(
    name="get_agent_memory",
    description=(
        "Get shared agent memory by topic. If no topic is specified, lists all available topics. "
        "When a topic is specified, returns its content and a generation number for optimistic "
        "locking. Pass the generation to store_agent_memory to safely handle concurrent updates."
    ),
)
async def get_agent_memory(topic: str | None = None) -> dict[str, Any]:
    """Get shared agent memory by topic.

    Agent memory is stored as topic-based markdown files in GCS. Each topic file contains
    learnings, patterns, or notes that agents accumulate over time.

    Args:
        topic: Topic name to read (supports nested folders like 'bookkeeper/routing-overview').
            If None, lists all available topics.

    Returns:
        Dictionary with topic content and generation (for optimistic locking),
        or list of available topics.
    """
    try:
        async with Storage() as client:
            if topic is None:
                result = await client.list_objects(
                    settings.AGENT_MEMORY_BUCKET, params={"prefix": _AGENT_MEMORY_PREFIX}
                )
                topics = []
                for item in result.get("items", []):
                    name = item["name"].removeprefix(_AGENT_MEMORY_PREFIX)
                    if name.endswith(".md"):
                        topics.append(name.removesuffix(".md"))
                return {"success": True, "topics": sorted(topics)}

            error = _validate_memory_topic(topic)
            if error:
                return {"success": False, "error": error}

            blob_path = f"{_AGENT_MEMORY_PREFIX}{topic}.md"

            try:
                metadata = await client.download_metadata(settings.AGENT_MEMORY_BUCKET, blob_path)
            except aiohttp.ClientResponseError as e:
                if e.status == 404:
                    return {
                        "success": True,
                        "topic": topic,
                        "content": None,
                        "generation": 0,
                        "message": "Topic does not exist yet. Use store_agent_memory to create it.",
                    }
                raise

            content_bytes = await client.download(settings.AGENT_MEMORY_BUCKET, blob_path)
            return {
                "success": True,
                "topic": topic,
                "content": content_bytes.decode("utf-8"),
                "generation": int(metadata["generation"]),
            }

    except Exception as e:
        logger.warning("Error getting agent memory", error=str(e), topic=topic)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="store_agent_memory",
    description=(
        "Store shared agent memory for a topic. Uses optimistic locking via expected_generation "
        "to prevent lost updates when multiple agents write concurrently. "
        "Pass the generation from get_agent_memory (use 0 to create a new topic). "
        "On conflict, re-read with get_agent_memory, merge your changes, and retry."
    ),
)
async def store_agent_memory(
    topic: str,
    content: str,
    expected_generation: int = 0,
    agent_name: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Store shared agent memory for a topic with optimistic locking.

    Uses GCS generation-match preconditions for compare-and-swap semantics:
    - Pass expected_generation=0 to create a new topic (fails if it already exists).
    - Pass expected_generation from get_agent_memory to update an existing topic.
    - On conflict (412 PreconditionFailed), the caller should re-read, merge, and retry.

    When creating a new topic (expected_generation=0), a YAML front-matter header with
    attribution (agent_name, session_id, created_at) is prepended automatically.

    When updating an existing topic, an HTML comment attribution marker is appended to
    the content so readers can see which agent contributed each section.

    Args:
        topic: Topic name. Supports nested folders using '/' (e.g., 'bookkeeper/routing-overview').
            Each segment must be alphanumeric with hyphens or underscores.
        content: Full markdown content for the topic file.
        expected_generation: Generation number from get_agent_memory for CAS.
        agent_name: Optional name of the agent writing this memory.
        session_id: Optional session ID for traceability.

    Returns:
        Dictionary with success status and new generation number.
    """
    try:
        error = _validate_memory_topic(topic)
        if error:
            return {"success": False, "error": error}

        if not content.strip():
            return {"success": False, "error": "Content cannot be empty"}

        # Inject attribution metadata
        has_attribution = agent_name or session_id
        if has_attribution:
            if expected_generation == 0:
                # New file: prepend YAML front-matter header
                header = _build_attribution_header(agent_name, session_id)
                content = f"{header}\n\n{content}"
            else:
                # Existing file: the caller provides the full merged content.
                # We append an attribution marker to the content so the update
                # is traceable, but only if the content doesn't already start
                # with the front-matter (i.e., the caller preserved the original header).
                marker = _build_attribution_marker(agent_name, session_id)
                # Insert marker at the very end so it doesn't break the front-matter
                content = f"{content}\n\n{marker}"

        content_size = len(content.encode("utf-8"))
        if content_size > _AGENT_MEMORY_MAX_TOPIC_SIZE_BYTES:
            max_kb = _AGENT_MEMORY_MAX_TOPIC_SIZE_BYTES // 1024
            return {
                "success": False,
                "error": f"Content size ({content_size} bytes) exceeds maximum of {max_kb}KB per topic. "
                "Prune outdated entries before adding new ones.",
            }

        blob_path = f"{_AGENT_MEMORY_PREFIX}{topic}.md"

        try:
            # Uses retry_gcs for automatic retries on transient GCS errors.
            # GCSPreconditionFailed (412) is raised without retry for caller to handle.
            upload_result = await _upload_agent_memory(
                bucket=settings.AGENT_MEMORY_BUCKET,
                blob_path=blob_path,
                content_bytes=content.encode("utf-8"),
                expected_generation=expected_generation,
            )
        except GCSPreconditionFailed:
            return {
                "success": False,
                "error": "CONFLICT",
                "message": (
                    f"Topic '{topic}' was modified by another agent since you last read it "
                    f"(expected generation {expected_generation}). "
                    "Re-read with get_agent_memory, merge your changes, and retry."
                ),
            }

        generation = int(upload_result["generation"])
        logger.info(
            "Agent memory stored",
            topic=topic,
            generation=generation,
            agent_name=agent_name,
            session_id=session_id,
        )

        # Fire-and-forget background indexing for search index
        from ypl.backend.llm.agent_memory_indexing import index_topic_sections
        from ypl.backend.utils.async_utils import create_background_task

        create_background_task(
            index_topic_sections(topic, content, agent_name=agent_name, source_generation=generation)
        )

        return {
            "success": True,
            "topic": topic,
            "generation": generation,
        }

    except Exception as e:
        logger.warning("Error storing agent memory", error=str(e), topic=topic)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="search_agent_memory",
    description=(
        "Search agent memory using semantic similarity, keyword matching, or hybrid (default). "
        "Returns matching sections ranked by relevance. "
        "Use scope to control what is searched: 'public' (shared team memory, default), "
        "'private' (your agent_memories/ directory only, requires agent_name), "
        "or 'all' (both public and private, requires agent_name). "
        "Note: results come from a search index that is eventually consistent with GCS source of truth."
    ),
)
async def search_agent_memory_tool(
    query: str,
    mode: str = "hybrid",
    top_k: int = 5,
    topic: str | None = None,
    full_content: bool = False,
    scope: str = "public",
    agent_name: str = "",
) -> dict[str, Any]:
    """Search agent memory sections with configurable scope.

    Args:
        query: Search query text (required, non-empty).
        mode: Search mode - "semantic", "keyword", or "hybrid" (default).
        top_k: Max results (1-20, default 5).
        topic: Optional topic filter.
        full_content: Return full section content instead of 500-char snippet.
        scope: "public" (shared memory, default), "private" (agent's own memory),
               or "all" (both). "private" and "all" require agent_name.
        agent_name: Agent name for private memory scoping (required for scope "private" or "all").
    """
    try:
        if not query or not query.strip():
            return {"success": False, "error": "Query must be non-empty."}

        valid_modes = ("semantic", "keyword", "hybrid")
        if mode not in valid_modes:
            return {"success": False, "error": f"Invalid mode '{mode}'. Must be one of: {valid_modes}"}

        valid_scopes = ("public", "private", "all")
        if scope not in valid_scopes:
            return {"success": False, "error": f"Invalid scope '{scope}'. Must be one of: {valid_scopes}"}

        clean_agent_name = agent_name.strip()
        if clean_agent_name and not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", clean_agent_name):
            return {
                "success": False,
                "error": f"Invalid agent_name '{agent_name}'. Must be alphanumeric with hyphens or underscores.",
            }
        if scope in ("private", "all") and not clean_agent_name:
            return {"success": False, "error": f"agent_name is required when scope='{scope}'."}

        if topic is not None:
            topic_error = _validate_memory_topic(topic)
            if topic_error:
                return {"success": False, "error": topic_error}

        from ypl.backend.llm.agent_memory_search import search_agent_memory

        results = await search_agent_memory(
            query=query.strip(),
            mode=mode,
            top_k=top_k,
            topic=topic,
            full_content=full_content,
            scope=scope,
            agent_name=clean_agent_name or None,
        )

        result: dict[str, Any] = {
            "success": True,
            "results": results,
            "count": len(results),
            "scope": scope,
        }
        if clean_agent_name:
            result["agent_name"] = clean_agent_name
        return result

    except Exception as e:
        logger.error("Error searching agent memory", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}
