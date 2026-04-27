"""MCP tools for Yupp development.

Tool functions are registered with FastMCP using the @mcp_server.tool() decorator
in their respective modules under ypl/mcp_server/tools/.

This module serves as the orchestrator:
- Registers skill resources from .agents/skills/
- Imports all tool modules to trigger @mcp_server.tool() registration
- Provides execute_tool() dispatcher for REST API endpoint (delegates to FastMCP)
- Provides format_tool_result() for formatting tool output
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastmcp.tools.tool import ToolResult

# Import tool modules to trigger @mcp_server.tool() registration.
# Each module self-registers its tools via the @mcp_server.tool() decorator at
# import time. Deleting any of these lines silently removes the corresponding
# tools from the agcouch MCP server — agent sessions would then get an empty
# tool list (as happened after PR #215). Do NOT remove without also deleting
# the tool module. Covered by tests/mcp_server/test_tool_registration.py.
import ypl.mcp_server.tools.agent_artifacts
import ypl.mcp_server.tools.agent_memory
import ypl.mcp_server.tools.agent_schedules
import ypl.mcp_server.tools.database
import ypl.mcp_server.tools.gcp_logs
import ypl.mcp_server.tools.linear_sync
import ypl.mcp_server.tools.memory_artifacts
import ypl.mcp_server.tools.project_tasks
import ypl.mcp_server.tools.redis
import ypl.mcp_server.tools.security_incidents
import ypl.mcp_server.tools.sentry
import ypl.mcp_server.tools.slack
import ypl.mcp_server.tools.twitter
from ypl.mcp_server.core import mcp_server
from ypl.structured_logger import get_logger
from ypl.utils import find_repo_root

logger = get_logger()

# ============================================================================
# MCP Resources - Dynamically loaded from .agents/skills/ directory
# ============================================================================

_REPO_ROOT = find_repo_root()
_SKILLS_DIR = _REPO_ROOT / ".agents" / "skills"


def _parse_skill_frontmatter(path: Path) -> dict[str, str]:
    """Extract YAML frontmatter from a SKILL.md file.

    Returns a dict with 'name' and 'description' keys (empty strings if missing).
    Frontmatter is delimited by ``---`` lines at the top of the file.
    """
    try:
        text = path.read_text()
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    result: dict[str, str] = {}
    for line in text[3:end].strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


def _register_skill_resources() -> None:
    """Register MCP resources from .agents/skills/ directory.

    Each subdirectory containing a SKILL.md file becomes a resource with URI:
    yupp://skills/{directory-name}

    If the SKILL.md has YAML frontmatter with a ``description`` field, it is
    used as the resource description (visible in the catalog injected into raw
    executor system prompts).
    """
    if not _SKILLS_DIR.exists():
        logger.warning("Skills directory not found", path=str(_SKILLS_DIR))
        return

    for skill_file in _SKILLS_DIR.glob("*/SKILL.md"):
        skill_name = skill_file.parent.name
        resource_uri = f"yupp://skills/{skill_name}"
        frontmatter = _parse_skill_frontmatter(skill_file)
        description = frontmatter.get("description", f"Skill: {skill_name}")

        # Create a closure to capture skill_file for each resource
        def make_resource_fn(path: Path, desc: str) -> Callable[[], str]:
            def resource_fn() -> str:
                return path.read_text()

            resource_fn.__doc__ = desc
            return resource_fn

        mcp_server.resource(resource_uri)(make_resource_fn(skill_file, description))
        logger.info("Registered skill resource", uri=resource_uri, file=str(skill_file))


# Register skill resources at module load time
_register_skill_resources()


# ============================================================================
# Tool Result Formatting & Dispatcher
# ============================================================================


def _tool_result_to_dict(tool_result: ToolResult) -> dict[str, Any]:
    """Convert a FastMCP ToolResult to a JSON-serializable dict.

    Extracts text content from ToolResult content blocks. If the text is valid
    JSON (which most of our tools return), parses it back to a dict. Otherwise
    returns the raw text in a wrapper dict.

    Note: Only text content blocks are extracted. Non-text blocks (images, resources)
    are currently not produced by any of our tools, so they are omitted.
    TODO: If tools start returning non-text content, extend this to handle those types.
    """
    text = "\n".join(block.text for block in tool_result.content if hasattr(block, "text"))

    # Most tools return JSON-serialized dicts — parse back to dict for the REST API.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    return {"result": text}


def format_tool_result(result: dict[str, Any] | ToolResult) -> str:
    """Format tool result as JSON string for REST API response."""
    if isinstance(result, ToolResult):
        result = _tool_result_to_dict(result)
    return json.dumps(result, indent=2, default=str)


async def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute a tool by name with the given arguments.

    Delegates to FastMCP's tool dispatch, which uses the registered @mcp_server.tool()
    functions directly. This ensures a single source of truth for tool schemas, defaults,
    and validation — no manual if/else mirroring required.

    Note: This goes through FastMCP's middleware chain, so ToolCallLoggingMiddleware
    will fire automatically. Callers should not duplicate audit logging.

    Args:
        name: Tool name to execute
        arguments: Tool arguments

    Returns:
        Tool result as a dictionary
    """
    from fastmcp.server.context import Context, _current_context

    # _call_tool_middleware is the correct entry point: it runs the full middleware chain
    # (including ToolCallLoggingMiddleware) then dispatches to the registered tool.
    # FastMCP does not expose a public call_tool() method (only _call_tool, _call_tool_mcp,
    # and _call_tool_middleware exist), so this is the intended internal dispatch path —
    # the same one used by the MCP transport itself.
    #
    # FastMCP requires an active Context in _current_context (a ContextVar) for its
    # middleware chain. When called via MCP transport, FastMCP sets this automatically.
    # For the REST endpoint, we must set it ourselves.
    existing = _current_context.get(None)
    if existing is not None:
        tool_result = await mcp_server._call_tool_middleware(name, arguments)
    else:
        ctx = Context(fastmcp=mcp_server)
        token = _current_context.set(ctx)
        try:
            tool_result = await mcp_server._call_tool_middleware(name, arguments)
        finally:
            _current_context.reset(token)
    return _tool_result_to_dict(tool_result)
