"""Thin client so the built-in artifact tools read/write **AL arti**
(``arti.voltcouch.com``) instead of the legacy AHS store.

Attribution reuses the external-MCP resolver: the caller's per-user arti grant
is sent as ``Authorization: Bearer`` when present, otherwise the ``arti`` server
row's M2M shared secret is used — so an interactive session is attributed to the
real user and an unattended one still succeeds via the service secret.

The internal tools call :func:`call_tool` with an arti MCP tool name + arguments
and get back the parsed result (arti returns a JSON string in
``result.content[0].text`` for artifact tools; we decode it).
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx
from sqlmodel import select

from ypl.backend.db import get_async_session
from ypl.db.external_mcp import McpServer
from ypl.external_mcp.resolver import _resolve_headers
from ypl.structured_logger import get_logger

logger = get_logger()

# Slug of the registry row that points at AL arti (see admin_external_mcps / the
# migration). The row carries the /mcp endpoint + OBO config + M2M fallback.
ARTI_SLUG = "arti"

# Public web base for building shareable artifact URLs (the registry row's URL is
# the internal /mcp endpoint, not the browser URL).
ARTI_PUBLIC_BASE_URL = os.environ.get("ARTI_PUBLIC_BASE_URL", "https://arti.voltcouch.com").rstrip("/")


class ArtiUnavailable(Exception):
    """arti isn't registered/enabled, or no usable credential for the caller."""


class ArtiToolError(Exception):
    """arti returned a JSON-RPC error (e.g. not found, forbidden, 409)."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


async def _endpoint_and_headers(
    user_id: str | None, agent_session_id: uuid.UUID | None = None
) -> tuple[str, dict[str, str]]:
    async with get_async_session() as session:
        srv = (
            await session.exec(
                select(McpServer).where(McpServer.slug == ARTI_SLUG, McpServer.enabled.is_(True))  # type: ignore[attr-defined]
            )
        ).first()
        if srv is None:
            raise ArtiUnavailable(f"MCP server {ARTI_SLUG!r} is not registered or not enabled")
        headers, reason = await _resolve_headers(
            session, srv=srv, user_id=user_id or "", agent_session_id=agent_session_id
        )
        if headers is None:
            raise ArtiUnavailable(reason or "no arti credential for caller")
        return srv.url, headers


def public_url(*, slug: str | None, artifact_id: str | None, version: int | None = None) -> str:
    """Build the browser URL for an arti artifact (slug preferred)."""
    if slug:
        return f"{ARTI_PUBLIC_BASE_URL}/s/{slug}" + (f"/{version}" if version else "")
    return f"{ARTI_PUBLIC_BASE_URL}/artifacts/{artifact_id}"


async def call_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    user_id: str | None,
    agent_session_id: uuid.UUID | None = None,
    timeout: float = 30.0,
) -> Any:
    """Call one arti MCP tool and return its parsed result.

    Raises :class:`ArtiUnavailable` when arti can't be reached/authorized and
    :class:`ArtiToolError` when arti returns a JSON-RPC error.
    """
    url, headers = await _endpoint_and_headers(user_id, agent_session_id)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers={**headers, "Content-Type": "application/json"}, json=payload)
    except httpx.HTTPError as exc:
        raise ArtiUnavailable(f"arti request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise ArtiUnavailable(f"arti HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        raise ArtiToolError(err.get("message", str(err)), code=err.get("code"))
    result = body.get("result", {}) if isinstance(body, dict) else {}
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list) and content and isinstance(content[0], dict) and content[0].get("type") == "text":
        text = content[0].get("text", "")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return {"text": text}
    return result


async def call_tool_raw(
    name: str,
    arguments: dict[str, Any],
    *,
    user_id: str | None,
    agent_session_id: uuid.UUID | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Like :func:`call_tool` but returns arti's raw JSON-RPC ``result`` dict
    without decoding ``content`` — needed for ``read_artifact``, whose content
    block carries the artifact body verbatim (not JSON) plus sibling fields
    (``content_type``, ``size_bytes``)."""
    url, headers = await _endpoint_and_headers(user_id, agent_session_id)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers={**headers, "Content-Type": "application/json"}, json=payload)
    except httpx.HTTPError as exc:
        raise ArtiUnavailable(f"arti request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise ArtiUnavailable(f"arti HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        raise ArtiToolError(err.get("message", str(err)), code=err.get("code"))
    return body.get("result", {}) if isinstance(body, dict) else {}


def not_found(exc: ArtiToolError) -> bool:
    """Heuristic: did arti report the artifact as missing? (reads should then
    fall back to the legacy store for un-migrated ids/slugs)."""
    msg = str(exc).lower()
    return "not found" in msg or "no such" in msg or exc.code == -32001
