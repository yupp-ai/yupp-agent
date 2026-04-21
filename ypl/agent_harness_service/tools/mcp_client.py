"""MCP client connections for raw executor tool access.

Manages fastmcp Client instances for the local ``harness`` MCP and the
first-class ``agcouch`` MCP, providing tool schemas and a unified tool
executor for the raw executor loop.

TODO(phase-9): once the DB-backed external MCP registry lands, extend
the session setup loop below to also open clients for any enabled rows
in ``mcp_servers`` beyond agcouch.
"""

import asyncio
from collections.abc import Mapping
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from ypl.agent_harness_service.common.constants import (
    AHS_MCP_BASE_URL,
    AHS_MCP_SECRET,
    ALL_MCP_SERVERS,
    PERM_DENY,
)
from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()

# Timeout for MCP tool calls (seconds). Long to accommodate new_task which spawns subagents.
_MCP_TIMEOUT_S = 600


def _strip_session_id_from_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove ``session_id`` from a JSON Schema so the LLM doesn't see it.

    The raw executor auto-injects ``session_id`` at call time, so there's no
    reason for the model to know about it. Removing it from the schema avoids
    confusion and wasted tokens.
    """
    props = schema.get("properties", {})
    if "session_id" not in props:
        return schema

    schema = {**schema, "properties": {k: v for k, v in props.items() if k != "session_id"}}

    required = schema.get("required")
    if isinstance(required, list) and "session_id" in required:
        schema["required"] = [r for r in required if r != "session_id"]

    return schema


class MCPToolAccess:
    """Manages MCP client connections for raw executor tool access.

    Usage as an async context manager:
        async with MCPToolAccess(session_id, agent_tools, allowed_servers=frozenset(ALL_MCP_SERVERS)) as mcp:
            tools = mcp.mcp_tools
            resources = mcp.resource_catalog
            result = await mcp.call_tool("list_agents", {})
    """

    def __init__(
        self,
        session_id: str | None,
        agent_tools: Mapping[str, str],
        allowed_servers: frozenset[str] = frozenset(ALL_MCP_SERVERS),
        user_id: str | None = None,
        agent_name: str = "",
    ) -> None:
        self._session_id = session_id or ""
        self._agent_tools = agent_tools
        self._allowed_servers = allowed_servers
        self._agent_name = agent_name
        self._user_id = user_id
        self._clients: list[Client] = []
        self._tool_registry: dict[str, Client] = {}  # tool_name -> client
        self._tool_schemas: list[dict[str, Any]] = []
        self._tools_needing_session_id: set[str] = set()  # tools that declared session_id
        self._resource_registry: dict[str, Client] = {}  # uri -> client
        self._resource_catalog: list[dict[str, str]] = []

    async def __aenter__(self) -> "MCPToolAccess":
        # If agent denies all tools, skip all connections
        if self._agent_tools.get("*") == PERM_DENY and len(self._agent_tools) == 1:
            return self

        # 1. Harness MCP (connect if allowed)
        if "harness" in self._allowed_servers:
            harness_url = f"{AHS_MCP_BASE_URL}/mcp/harness/"
            harness_headers = {
                "X-AHS-Token": AHS_MCP_SECRET,
                "X-AHS-Session-ID": self._session_id,
            }
            await self._connect("harness", harness_url, harness_headers)

        # 2. Agcouch MCP — first-class remote MCP shipped with this repo.
        # Only connect if: enabled, allowed by the agent's server list, and
        # a bearer token is configured.
        if settings.AGCOUCH_MCP_ENABLED and settings.AGCOUCH_MCP_SERVER_NAME in self._allowed_servers:
            agcouch_mcp_token = settings.AGCOUCH_MCP_TOKEN
            agcouch_mcp_url = settings.AGCOUCH_MCP_SERVER_URL
            if not agcouch_mcp_token:
                logger.warning(
                    "Agcouch MCP skipped: AGCOUCH_MCP_TOKEN not set",
                    session_id=self._session_id,
                )
            elif not agcouch_mcp_url:
                logger.warning(
                    "Agcouch MCP skipped: AGCOUCH_MCP_SERVER_URL not set",
                    session_id=self._session_id,
                )
            else:
                agcouch_mcp_headers: dict[str, str] = {"Authorization": f"Bearer {agcouch_mcp_token}"}
                if self._user_id:
                    agcouch_mcp_headers["X-User-ID"] = self._user_id
                if self._agent_name:
                    agcouch_mcp_headers["X-AHS-Agent-Name"] = self._agent_name
                if self._session_id:
                    agcouch_mcp_headers["X-AHS-Session-ID"] = self._session_id
                await self._connect(settings.AGCOUCH_MCP_SERVER_NAME, agcouch_mcp_url, agcouch_mcp_headers)

        # TODO(phase-9): iterate DB-registered external MCP servers here
        # and open a client for each enabled row using the same pattern.

        # Register read_resource synthetic tool if any resources were discovered
        if self._resource_catalog:
            self._tool_schemas.append(
                {
                    "name": "read_resource",
                    "description": (
                        "Read an MCP resource by URI. See the 'Available Resources' "
                        "section in the system prompt for the list of URIs."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "uri": {
                                "type": "string",
                                "description": "Resource URI (e.g., 'yupp://skills/fetch-from-db')",
                            },
                        },
                        "required": ["uri"],
                    },
                }
            )

        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._clients:
            results = await asyncio.gather(
                *(client.__aexit__(None, None, None) for client in self._clients),  # type: ignore[no-untyped-call]
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    logger.warning("Error closing MCP client", exc_info=result)
        self._clients.clear()
        self._tool_registry.clear()
        self._tool_schemas.clear()
        self._tools_needing_session_id.clear()
        self._resource_registry.clear()
        self._resource_catalog.clear()

    @property
    def mcp_tools(self) -> list[dict[str, Any]]:
        """Tool schemas in MCP dict format (name, description, inputSchema)."""
        return self._tool_schemas

    @property
    def resource_catalog(self) -> list[dict[str, str]]:
        """Discovered MCP resources (uri, name, description, mimeType)."""
        return self._resource_catalog

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        """Route a tool call to the correct MCP client and return text result.

        Automatically injects ``session_id`` for harness tools that require it,
        so the LLM never needs to know or provide the value.
        Handles the synthetic ``read_resource`` tool by routing to the appropriate
        MCP client's resource read method.
        """
        # Handle synthetic read_resource tool
        if name == "read_resource":
            return await self._read_resource(args.get("uri", ""))

        client = self._tool_registry.get(name)
        if not client:
            return f"[ERROR] Unknown tool: {name!r}"

        # Always overwrite session_id for tools that declared it — prevents the
        # model from passing a crafted session_id to access another session's workspace.
        # Only inject for tools that originally had session_id in their schema.
        if self._session_id and name in self._tools_needing_session_id:
            args = {**args, "session_id": self._session_id}

        result = await client.call_tool(name, args, timeout=_MCP_TIMEOUT_S)

        # Extract text from CallToolResult content blocks
        parts: list[str] = []
        for block in result.content:
            if block.type == "text":
                parts.append(block.text)
            else:
                # For non-text content (images, etc.), include a placeholder
                parts.append(f"[{block.type} content]")

        text = "\n".join(parts) if parts else "(empty result)"

        # Surface tool-level errors so the model can distinguish failures
        if getattr(result, "isError", False):
            return f"[ERROR] {text}"
        return text

    async def _read_resource(self, uri: str) -> str:
        """Read an MCP resource by URI."""
        if not uri:
            return "[ERROR] uri is required"
        client = self._resource_registry.get(uri)
        if not client:
            available = [r["uri"] for r in self._resource_catalog]
            return f"[ERROR] Unknown resource URI: {uri!r}. Available: {available}"
        try:
            # fastmcp Client.read_resource returns list[TextResourceContents | BlobResourceContents]
            contents = await client.read_resource(uri)
            parts: list[str] = []
            for block in contents:
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif hasattr(block, "blob"):
                    parts.append(f"[binary content, {len(block.blob)} bytes]")
                else:
                    parts.append(str(block))
            return "\n".join(parts) if parts else "(empty resource)"
        except Exception as e:
            logger.error("Failed to read MCP resource", uri=uri, error=str(e), exc_info=True)
            return f"[ERROR] Failed to read resource {uri!r}: {e}"

    async def _connect(self, server_name: str, url: str, headers: dict[str, str]) -> None:
        """Connect to an MCP server, fetch tool schemas, and register tools."""
        transport = StreamableHttpTransport(url=url, headers=headers)
        client = Client(transport=transport, timeout=_MCP_TIMEOUT_S)

        try:
            await client.__aenter__()  # type: ignore[no-untyped-call]
        except Exception:
            logger.error("Failed to connect to MCP server", server=server_name, url=url, exc_info=True)
            return

        self._clients.append(client)

        try:
            tools = await client.list_tools()
        except Exception:
            logger.error("Failed to list tools from MCP server", server=server_name, exc_info=True)
            return

        registered = 0
        for tool in tools:
            tool_name = tool.name
            # Harness tools take precedence on name collision
            if tool_name in self._tool_registry:
                logger.debug("Skipping duplicate tool", tool=tool_name, server=server_name)
                continue
            # Apply agent-level tool permissions (single enforcement point).
            # If the tool is explicitly denied, skip it entirely.
            perm = self._agent_tools.get(tool_name, self._agent_tools.get("*", ""))
            if perm == PERM_DENY:
                continue
            self._tool_registry[tool_name] = client

            # Strip session_id from tool schemas — auto-injected at call time
            # (see call_tool). Track which tools had it so we only inject for those.
            raw_schema = tool.inputSchema or {"type": "object", "properties": {}}
            if "session_id" in raw_schema.get("properties", {}):
                self._tools_needing_session_id.add(tool_name)
            schema = _strip_session_id_from_schema(raw_schema)

            self._tool_schemas.append(
                {
                    "name": tool_name,
                    "description": tool.description or "",
                    "inputSchema": schema,
                }
            )
            registered += 1

        logger.info(
            "Connected to MCP server",
            server=server_name,
            tool_count=registered,
            total_available=len(tools),
        )

        # Discover MCP resources from this server
        try:
            resources = await client.list_resources()
        except Exception:
            logger.debug("Server does not support resources or list failed", server=server_name)
            resources = []

        resource_count = 0
        for res in resources:
            uri = str(res.uri)
            if uri in self._resource_registry:
                continue
            self._resource_registry[uri] = client
            self._resource_catalog.append(
                {
                    "uri": uri,
                    "name": res.name or uri.rsplit("/", 1)[-1],
                    "description": res.description or "",
                    "mimeType": getattr(res, "mimeType", "text/plain") or "text/plain",
                }
            )
            resource_count += 1

        if resource_count:
            logger.info(
                "Discovered MCP resources",
                server=server_name,
                resource_count=resource_count,
            )
