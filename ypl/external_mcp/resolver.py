"""Inject external MCPs into a session's ``.mcp.json``.

Called from :mod:`ypl.agent_harness_service.executors.mcp_config` once the
baseline servers have been resolved.  We pull the list of MCP slugs the
agent declared in its config, intersect with what the user is allowed to
attach (RBAC roles ∩ ``mcp_server_roles``) and the agent's allowlist
(``mcp_server_agents``), resolve each grant to a bearer token, and emit
one ``mcpServers`` entry per server.

When a user is missing a grant, or a refresh fails, we don't fail the
turn — we just omit that server and append a one-line ``unavailable_mcps``
field to the resolver result so the harness can warn the agent via the
SYSTEM channel.
"""

from __future__ import annotations
import uuid
from typing import Any

import sqlalchemy as sa
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ypl.backend.db import get_async_session
from ypl.db.external_mcp import (
    McpAuthType,
    McpServer,
    McpServerAgent,
    McpServerRoleAccess,
    McpTransport,
)
from ypl.db.rbac import UserRoleAssociation
from ypl.external_mcp import grants as grants_svc
from ypl.structured_logger import get_logger

logger = get_logger()


async def _user_role_ids(session: AsyncSession, user_id: str) -> set[uuid.UUID]:
    rows = (await session.exec(select(UserRoleAssociation.role_id).where(UserRoleAssociation.user_id == user_id))).all()
    return set(rows)


async def _allowed_servers_for(
    session: AsyncSession,
    *,
    user_id: str,
    agent_id: uuid.UUID | None,
    requested_slugs: list[str],
) -> list[McpServer]:
    """Return the McpServer rows the agent+user pair may attach, in the
    order of ``requested_slugs``."""
    if not requested_slugs:
        return []

    user_roles = await _user_role_ids(session, user_id)
    servers = (
        await session.exec(
            select(McpServer).where(
                sa.and_(McpServer.slug.in_(requested_slugs), McpServer.enabled.is_(True))  # type: ignore[attr-defined]
            )
        )
    ).all()

    allowed: list[McpServer] = []
    for srv in servers:
        # Role gate: user's roles must intersect the server's allowed roles.
        role_rows = (
            await session.exec(
                select(McpServerRoleAccess.role_id).where(McpServerRoleAccess.mcp_server_id == srv.mcp_server_id)
            )
        ).all()
        allowed_roles = set(role_rows)
        if allowed_roles and not (user_roles & allowed_roles):
            logger.info("MCP role-gated for user", slug=srv.slug, user_id=user_id)
            continue
        # Agent gate (optional).  Empty list = available to every agent.
        agent_rows = (
            await session.exec(select(McpServerAgent.agent_id).where(McpServerAgent.mcp_server_id == srv.mcp_server_id))
        ).all()
        allowed_agents = set(agent_rows)
        if allowed_agents and (agent_id is None or agent_id not in allowed_agents):
            logger.info("MCP agent-gated", slug=srv.slug, agent_id=str(agent_id))
            continue
        allowed.append(srv)

    # Preserve agent-config order
    by_slug = {s.slug: s for s in allowed}
    return [by_slug[s] for s in requested_slugs if s in by_slug]


def _transport_type(srv: McpServer) -> str:
    """Map our enum onto the string Claude Code / Codex expect in ``.mcp.json``."""
    if srv.transport == McpTransport.SSE:
        return "sse"
    return "http"


async def build_external_mcp_entries(
    *,
    user_id: str,
    agent_id: uuid.UUID | None,
    requested_slugs: list[str],
    agent_session_id: uuid.UUID | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Return ``(servers_dict, unavailable)``.

    ``servers_dict`` maps slug → MCP-json entry (ready to merge into the
    ``mcpServers`` block).  ``unavailable`` lists ``{slug, reason}`` items
    the caller can surface to the agent ("you asked for gmail but the user
    hasn't connected it yet — point them at https://lit.<apex>/my_mcps").
    """
    if not requested_slugs:
        return {}, []

    servers_out: dict[str, Any] = {}
    unavailable: list[dict[str, str]] = []

    async with get_async_session() as session:
        servers = await _allowed_servers_for(
            session, user_id=user_id, agent_id=agent_id, requested_slugs=requested_slugs
        )
        allowed_slugs = {s.slug for s in servers}
        unavailable.extend(
            {"slug": slug, "reason": "not allowed or disabled"} for slug in requested_slugs if slug not in allowed_slugs
        )

        for srv in servers:
            if not srv.allow_token_to_agent and srv.auth_type != McpAuthType.NONE:
                # Phase-2 will introduce a sidecar proxy; until then this is
                # a hard refusal so we don't leak a sensitive token.
                unavailable.append({"slug": srv.slug, "reason": "token-to-agent disabled (proxy not yet supported)"})
                continue

            bearer = await grants_svc.resolve_bearer(
                session, user_id=user_id, server=srv, agent_session_id=agent_session_id
            )
            if bearer is None:
                unavailable.append({"slug": srv.slug, "reason": "no active grant — connect at /my_mcps"})
                continue

            entry: dict[str, Any] = {
                "type": _transport_type(srv),
                "url": srv.url,
            }
            if bearer:  # NONE auth returns ""
                token_type = "Bearer"
                entry["headers"] = {"Authorization": f"{token_type} {bearer}"}
            servers_out[srv.slug] = entry

        await session.commit()

    if unavailable:
        logger.info(
            "External MCPs unavailable for session",
            user_id=user_id,
            unavailable=unavailable,
        )
    return servers_out, unavailable
