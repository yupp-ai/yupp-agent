"""Sandboxed filesystem and web operation tools for the harness MCP server.

Provides read/write/edit/glob/grep/bash/webfetch/websearch tools that operate
within the agent's sandboxed workspace, with per-session rate limiting for
web searches.
"""

from __future__ import annotations
import asyncio

from fastmcp.utilities.types import File, Image

from ypl.agent_harness_service.common.constants import (
    MAX_WEBSEARCH_CALLS_PER_SESSION,
    MAX_WEBSEARCH_CALLS_PER_TURN,
)
from ypl.agent_harness_service.tools.mcp_instance import (
    _session_sandbox,
    _session_websearch_count,
    _session_websearch_locks,
    _turn_websearch_count,
    _validate_session_id,
    mcp,
)
from ypl.agent_harness_service.tools.workspace_tools import (
    BinaryFileResult,
    edit_file,
    fetch_url,
    get_command_handler_manager,
    list_files,
    read_file,
    run_command,
    search_files,
    search_web,
    write_file,
)
from ypl.structured_logger import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# Binary file helper
# ---------------------------------------------------------------------------

# Normalize extensions to the MIME subtype that FastMCP expects.
# FastMCP derives MIME mechanically: Image → image/{fmt}, File → application/{fmt}.
# Without this mapping, e.g. "jpg" → "image/jpg" (wrong) or "docx" → "application/docx" (wrong).
_EXT_TO_MIME_SUFFIX: dict[str, str] = {
    # Images
    "jpg": "jpeg",
    # Documents — deterministic; mimetypes.guess_type() is environment-dependent
    "doc": "msword",
    "docx": "vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "vnd.ms-excel",
    "xlsx": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "vnd.ms-powerpoint",
    "pptx": "vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _wrap_binary_result(result: BinaryFileResult) -> Image | File:
    """Convert a BinaryFileResult into the appropriate FastMCP content type.

    Image → MCP ImageContent      (model sees natively via vision)
    File  → MCP EmbeddedResource  (client-dependent; works for PDFs on Claude)
    """
    fmt = _EXT_TO_MIME_SUFFIX.get(result.extension, result.extension)
    if result.category == "image":
        return Image(data=result.data, format=fmt)
    return File(data=result.data, format=fmt)


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="read",
    description=(
        "Read a file from the workspace. Returns file contents with line numbers "
        "(cat -n style) for text files. For binary files that AI models can process "
        "(images, PDFs, documents), returns the content in a native multimodal "
        "format. Supports offset and limit for large text files."
    ),
)
def mcp_read(session_id: str, path: str, offset: int = 0, limit: int = 2000) -> str | Image | File:
    """Read a file from the workspace.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        path: File path relative to workspace root.
        offset: Line number to start from (0-based). Default 0. Ignored for binary files.
        limit: Maximum number of lines to read. Default 2000. Ignored for binary files.
    """
    logger.info("MCP tool: read", session_id=session_id, path=path)
    _validate_session_id(session_id)
    result = read_file(session_id, path, offset, limit)
    if isinstance(result, BinaryFileResult):
        return _wrap_binary_result(result)
    return result


@mcp.tool(
    name="write",
    description=(
        "Write or create a file in the workspace. Creates parent directories "
        "as needed. Requires write access (call request_write_access first)."
    ),
)
def mcp_write(session_id: str, path: str, content: str) -> str:
    """Write or create a file.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        path: File path relative to workspace root.
        content: File content to write.
    """
    logger.info("MCP tool: write", session_id=session_id, path=path, content_length=len(content))
    _validate_session_id(session_id)
    return write_file(session_id, path, content)


@mcp.tool(
    name="edit",
    description=(
        "Edit a file using find-and-replace. Finds old_string in the file and "
        "replaces it with new_string. Errors if old_string is not found or not "
        "unique (unless replace_all is True). Requires write access."
    ),
)
def mcp_edit(session_id: str, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Edit a file via find-and-replace.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        path: File path relative to workspace root.
        old_string: Text to find.
        new_string: Replacement text.
        replace_all: Replace all occurrences (default False).
    """
    logger.info("MCP tool: edit", session_id=session_id, path=path, replace_all=replace_all)
    _validate_session_id(session_id)
    return edit_file(session_id, path, old_string, new_string, replace_all)


@mcp.tool(
    name="glob",
    description=(
        "List files matching a glob pattern in the workspace. "
        "Supports patterns like '**/*.py' or 'src/**/*.ts'. "
        "Returns newline-separated relative file paths."
    ),
)
def mcp_glob(session_id: str, pattern: str, path: str | None = None) -> str:
    """List files matching a glob pattern.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        pattern: Glob pattern to match (e.g., '**/*.py').
        path: Optional subdirectory to search in.
    """
    logger.info("MCP tool: glob", session_id=session_id, pattern=pattern, path=path)
    _validate_session_id(session_id)
    return list_files(session_id, pattern, path)


@mcp.tool(
    name="grep",
    description=(
        "Search file contents using a regex pattern (grep-like). "
        "Returns matching lines in file:line:content format. "
        "Use the glob parameter to filter which files to search."
    ),
)
def mcp_grep(session_id: str, pattern: str, path: str | None = None, glob: str | None = None) -> str:
    """Search file contents with regex.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        pattern: Regex pattern to search for.
        path: Optional subdirectory to search in.
        glob: Optional glob pattern to filter files (e.g., '*.py').
    """
    logger.info("MCP tool: grep", session_id=session_id, pattern=pattern, path=path)
    _validate_session_id(session_id)
    return search_files(session_id, pattern, path, glob)


@mcp.tool(
    name="bash",
    description=(
        "Run a shell command in the workspace. Returns combined stdout+stderr. "
        "Commands run with bash in the workspace directory. "
        "Requires write access (call request_write_access first). "
        "Output is truncated at 100KB. Timeout default is 120s (max 600s)."
    ),
)
async def mcp_bash(session_id: str, command: str, timeout: int = 120) -> str:
    """Run a shell command.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        command: Shell command to execute.
        timeout: Timeout in seconds (default 120, max 600).
    """
    logger.info("MCP tool: bash", session_id=session_id, command_length=len(command), timeout=timeout)
    _validate_session_id(session_id)

    # Use the BCH warm proxy when registered for this session — avoids a fresh
    # bwrap spawn per call (~150–400 ms) and reuses the warm process (~5 ms).
    manager = get_command_handler_manager(session_id)
    if manager is not None:
        return await manager.call_tool("Bash", {"command": command, "timeout": timeout})

    # Fallback: original per-call bwrap path.
    stack = _session_sandbox.get(session_id)
    bwrap = stack[-1] if stack else False
    return run_command(session_id, command, timeout, bwrap=bwrap)


@mcp.tool(
    name="webfetch",
    description=(
        "Fetch content from a URL. Returns the page content as plain text "
        "(HTML tags are stripped). Useful for reading documentation, APIs, etc."
    ),
)
async def mcp_webfetch(session_id: str, url: str, prompt: str | None = None) -> str:
    """Fetch URL content.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        url: The URL to fetch.
        prompt: Optional prompt (unused, for API compatibility).
    """
    logger.info("MCP tool: webfetch", session_id=session_id, url=url)
    _validate_session_id(session_id)
    return await fetch_url(url, prompt)


@mcp.tool(
    name="websearch",
    description=(
        "Search the web using Google. Returns organic search results with "
        "titles, URLs, and snippets. Use this to find current information, "
        "documentation, or answers to questions."
    ),
)
async def mcp_websearch(session_id: str, query: str, num_results: int = 10) -> str:
    """Search the web.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        query: Search query string.
        num_results: Maximum number of results to return (default 10).
    """
    logger.info("MCP tool: websearch", session_id=session_id, query=query)
    _validate_session_id(session_id)

    # dict.setdefault is atomic (single bytecode op, no await) so exactly one Lock
    # is created per session_id even under concurrent calls.
    lock = _session_websearch_locks.setdefault(session_id, asyncio.Lock())

    async with lock:
        turn_count = _turn_websearch_count.get(session_id, 0)
        if turn_count >= MAX_WEBSEARCH_CALLS_PER_TURN:
            raise ValueError(
                f"Web search limit reached ({MAX_WEBSEARCH_CALLS_PER_TURN} per turn). "
                "Please work with the results you already have."
            )

        session_count = _session_websearch_count.get(session_id, 0)
        if session_count >= MAX_WEBSEARCH_CALLS_PER_SESSION:
            raise ValueError(
                f"Web search limit reached ({MAX_WEBSEARCH_CALLS_PER_SESSION} per session). "
                "Please work with the results you already have."
            )

        _turn_websearch_count[session_id] = turn_count + 1
        _session_websearch_count[session_id] = session_count + 1

    success = False
    try:
        result = await search_web(query, num_results)
        success = True
        return result
    finally:
        if not success:
            # Roll back counters on any failure (including CancelledError).
            async with lock:
                _turn_websearch_count[session_id] = max(0, _turn_websearch_count.get(session_id, 1) - 1)
                _session_websearch_count[session_id] = max(0, _session_websearch_count.get(session_id, 1) - 1)
