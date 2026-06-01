"""Grant lifecycle service.

One place to put / get / refresh / revoke ``McpUserGrant`` rows.  Both the
FastAPI OAuth callback and the runtime resolver call into this module so
encryption + audit-log emission stay in lockstep.

Functions:

- :func:`upsert_oauth_grant`  — write a fresh OAuth grant (post-callback).
- :func:`upsert_m2m_grant`    — write a per-user M2M API key.
- :func:`get_active_grant`    — look up the live grant for (user, server).
- :func:`resolve_bearer`      — return the bearer token to send to the MCP
                                  (refreshes OAuth tokens if near expiry).
- :func:`revoke_grant`        — soft-delete an active grant.

All writes append to ``mcp_grant_events``.
"""

from __future__ import annotations
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from ypl.db.external_mcp import (
    McpAuthType,
    McpGrantEvent,
    McpGrantEventType,
    McpServer,
    McpServerSecrets,
    McpUserGrant,
)
from ypl.external_mcp import crypto, oauth_client
from ypl.structured_logger import get_logger

logger = get_logger()

# Refresh tokens slightly before they actually expire so we don't race a
# concurrent agent call that grabs an expired token.
_REFRESH_SAFETY_WINDOW = timedelta(seconds=60)


async def _log_event(
    session: AsyncSession,
    *,
    user_id: str,
    mcp_server_id: uuid.UUID,
    event_type: McpGrantEventType,
    agent_session_id: uuid.UUID | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    session.add(
        McpGrantEvent(
            user_id=user_id,
            mcp_server_id=mcp_server_id,
            event_type=event_type,
            agent_session_id=agent_session_id,
            meta=meta or None,
        )
    )


async def _revoke_existing_active(session: AsyncSession, user_id: str, mcp_server_id: uuid.UUID) -> None:
    """Soft-revoke any currently-active grant for (user, server) so the
    partial unique index ``uq_mcp_user_grants_active`` stays happy when we
    insert a new one."""
    stmt = (
        select(McpUserGrant)
        .where(col(McpUserGrant.user_id) == user_id)
        .where(col(McpUserGrant.mcp_server_id) == mcp_server_id)
        .where(col(McpUserGrant.revoked_at).is_(None))
    )
    result = await session.exec(stmt)
    for row in result.all():
        row.revoked_at = datetime.now(UTC)
        session.add(row)


async def upsert_oauth_grant(
    session: AsyncSession,
    *,
    user_id: str,
    mcp_server_id: uuid.UUID,
    access_token: str,
    refresh_token: str | None,
    token_type: str,
    expires_at: datetime | None,
    scopes: list[str] | None,
) -> McpUserGrant:
    """Write a brand-new OAuth grant, retiring any prior active row."""
    await _revoke_existing_active(session, user_id, mcp_server_id)
    grant = McpUserGrant(
        user_id=user_id,
        mcp_server_id=mcp_server_id,
        access_token_enc=crypto.encrypt(access_token),
        refresh_token_enc=crypto.encrypt(refresh_token) if refresh_token else None,
        token_type=token_type,
        expires_at=expires_at,
        scopes=scopes,
    )
    session.add(grant)
    await _log_event(
        session,
        user_id=user_id,
        mcp_server_id=mcp_server_id,
        event_type=McpGrantEventType.GRANTED,
        meta={"scopes": scopes, "has_refresh_token": bool(refresh_token)},
    )
    return grant


async def upsert_m2m_grant(
    session: AsyncSession,
    *,
    user_id: str,
    mcp_server_id: uuid.UUID,
    api_key: str,
) -> McpUserGrant:
    """Write a per-user M2M grant (user pasted their own bearer key)."""
    await _revoke_existing_active(session, user_id, mcp_server_id)
    grant = McpUserGrant(
        user_id=user_id,
        mcp_server_id=mcp_server_id,
        api_key_enc=crypto.encrypt(api_key),
        token_type="Bearer",
    )
    session.add(grant)
    await _log_event(
        session,
        user_id=user_id,
        mcp_server_id=mcp_server_id,
        event_type=McpGrantEventType.GRANTED,
        meta={"auth": "M2M_PER_USER"},
    )
    return grant


async def get_active_grant(session: AsyncSession, *, user_id: str, mcp_server_id: uuid.UUID) -> McpUserGrant | None:
    stmt = (
        select(McpUserGrant)
        .where(col(McpUserGrant.user_id) == user_id)
        .where(col(McpUserGrant.mcp_server_id) == mcp_server_id)
        .where(col(McpUserGrant.revoked_at).is_(None))
    )
    return (await session.exec(stmt)).first()


async def revoke_grant(session: AsyncSession, *, user_id: str, mcp_server_id: uuid.UUID) -> bool:
    """Soft-revoke the active grant.  Returns whether one was found."""
    grant = await get_active_grant(session, user_id=user_id, mcp_server_id=mcp_server_id)
    if grant is None:
        return False
    grant.revoked_at = datetime.now(UTC)
    session.add(grant)
    await _log_event(
        session,
        user_id=user_id,
        mcp_server_id=mcp_server_id,
        event_type=McpGrantEventType.REVOKED,
    )
    return True


async def _refresh_oauth(
    session: AsyncSession,
    *,
    grant: McpUserGrant,
    server: McpServer,
    client_secret: str,
) -> str | None:
    """Refresh the access token in place.  Returns the new access token, or
    ``None`` if refresh failed (caller surfaces this to the user)."""
    if not grant.refresh_token_enc or not server.oauth_config:
        return None
    refresh = crypto.decrypt(grant.refresh_token_enc)
    if refresh is None:
        return None
    try:
        payload = await oauth_client.refresh_access_token(
            token_url=server.oauth_config["token_url"],
            client_id=server.oauth_config["client_id"],
            client_secret=client_secret,
            refresh_token=refresh,
        )
    except Exception as e:
        # Stamp the grant so the UI can surface "reconnect needed" without
        # waiting for the next agent turn to fail.
        grant.last_refresh_error = str(e)[:500]
        session.add(grant)
        await _log_event(
            session,
            user_id=grant.user_id,
            mcp_server_id=grant.mcp_server_id,
            event_type=McpGrantEventType.REFRESH_FAILED,
            meta={"error": str(e)[:500]},
        )
        logger.warning(
            "MCP refresh_token failed",
            mcp_server_id=str(grant.mcp_server_id),
            user_id=grant.user_id,
            error=str(e),
        )
        return None

    new_access: str = payload["access_token"]
    grant.access_token_enc = crypto.encrypt(new_access)
    if payload.get("refresh_token"):
        grant.refresh_token_enc = crypto.encrypt(payload["refresh_token"])
    grant.expires_at = oauth_client.expiry_from_expires_in(payload.get("expires_in"))
    grant.last_refresh_error = None
    session.add(grant)
    await _log_event(
        session,
        user_id=grant.user_id,
        mcp_server_id=grant.mcp_server_id,
        event_type=McpGrantEventType.REFRESHED,
    )
    return new_access


async def resolve_bearer(
    session: AsyncSession,
    *,
    user_id: str,
    server: McpServer,
    agent_session_id: uuid.UUID | None = None,
) -> str | None:
    """Return the bearer token (sans ``Bearer `` prefix) to attach when
    talking to ``server`` on behalf of ``user_id``.  Returns ``None`` when:

    - server is OAUTH_OBO/M2M_PER_USER and the user has no active grant,
    - the OAuth refresh attempt failed,
    - the grant's encrypted material couldn't be decrypted (rare).

    For M2M_SHARED, the token comes from ``mcp_server_secrets`` and is
    independent of the user.  For NONE, returns the literal empty string
    (caller should still send no Authorization header).
    """
    if server.auth_type == McpAuthType.NONE:
        return ""

    if server.auth_type == McpAuthType.M2M_SHARED:
        secrets = await session.get(McpServerSecrets, server.mcp_server_id)
        if not secrets or not secrets.m2m_shared_token_enc:
            return None
        return crypto.decrypt(secrets.m2m_shared_token_enc)

    grant = await get_active_grant(session, user_id=user_id, mcp_server_id=server.mcp_server_id)
    if grant is None:
        return None

    if server.auth_type == McpAuthType.M2M_PER_USER:
        if not grant.api_key_enc:
            return None
        grant.last_used_at = datetime.now(UTC)
        session.add(grant)
        await _log_event(
            session,
            user_id=user_id,
            mcp_server_id=server.mcp_server_id,
            event_type=McpGrantEventType.USED,
            agent_session_id=agent_session_id,
        )
        return crypto.decrypt(grant.api_key_enc)

    # OAUTH_OBO path
    if not grant.access_token_enc:
        return None
    needs_refresh = (
        grant.expires_at is not None and grant.expires_at - datetime.now(UTC) < _REFRESH_SAFETY_WINDOW
    )
    if needs_refresh:
        secrets = await session.get(McpServerSecrets, server.mcp_server_id)
        client_secret_enc = secrets.oauth_client_secret_enc if secrets else None
        client_secret = crypto.decrypt(client_secret_enc) if client_secret_enc else None
        if not client_secret:
            logger.error(
                "OAUTH_OBO server has no client_secret; cannot refresh",
                mcp_server_id=str(server.mcp_server_id),
                slug=server.slug,
            )
            return None
        new = await _refresh_oauth(session, grant=grant, server=server, client_secret=client_secret)
        if new is None:
            return None
        access: str | None = new
    else:
        access = crypto.decrypt(grant.access_token_enc)
    if access is None:
        return None
    grant.last_used_at = datetime.now(UTC)
    session.add(grant)
    await _log_event(
        session,
        user_id=user_id,
        mcp_server_id=server.mcp_server_id,
        event_type=McpGrantEventType.USED,
        agent_session_id=agent_session_id,
    )
    return access
