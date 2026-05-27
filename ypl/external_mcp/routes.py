"""FastAPI router that drives the per-user OAuth flow.

Mounted by ``ypl/mono_server/server.py`` at ``/mcp_oauth`` so the final URLs
are:

  GET  /mcp_oauth/start?server=<slug>&return_to=<url>
  GET  /mcp_oauth/callback?code&state
  POST /mcp_oauth/revoke?server=<slug>
  POST /mcp_oauth/m2m/set?server=<slug>   body: {"api_key": "..."}

The flow is anchored on the public AHS host (e.g.
``https://ahs.voltcouch.com``) because the provider's redirect_uri must be
stable + HTTPS.  Streamlit (Lit) opens ``/mcp_oauth/start`` in a new tab;
once the callback finishes, it redirects the browser back to ``return_to``
(typically the ``my_mcps`` Lit page) so the user lands where they came from.

Authentication for the *user* — that is, "who is connecting?" — is taken
from a signed token the Lit page issues right before the redirect.  See
:func:`ypl.external_mcp.oauth_client.sign_state`; the same JWT carries
``(user_id, server_slug, return_to)`` so the callback can resolve everything
without a side channel.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlencode

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlmodel import select

from ypl.backend.db import get_async_session
from ypl.db.external_mcp import McpAuthType, McpServer, McpServerSecrets
from ypl.external_mcp import crypto, grants, oauth_client
from ypl.structured_logger import get_logger

logger = get_logger()

router = APIRouter(tags=["external-mcp-oauth"])


def _public_base_url() -> str:
    """The host this service is reachable at from the outside world.

    Used to build the OAuth ``redirect_uri`` we hand to the provider.
    Falls back to ``GATEWAY_BASE_URL`` (already set by the Mac installer
    to ``https://ahs.<apex>``) and finally to the request URL the caller
    arrived on.
    """
    return os.environ.get("GATEWAY_BASE_URL", "").rstrip("/")


def _redirect_uri(request: Request) -> str:
    base = _public_base_url() or str(request.base_url).rstrip("/")
    return f"{base}/mcp_oauth/callback"


async def _get_server_by_slug(slug: str) -> McpServer:
    async with get_async_session() as session:
        srv = (await session.exec(select(McpServer).where(McpServer.slug == slug))).first()
    if srv is None or not srv.enabled:
        raise HTTPException(status_code=404, detail=f"Unknown or disabled MCP: {slug!r}")
    return srv


@router.get("/start")
async def start_oauth(
    request: Request,
    server: str = Query(..., description="MCP slug to connect"),
    user_id: str = Query(..., description="Lit-resolved user_id of the connecting user"),
    return_to: str = Query("", description="URL to land on after the callback"),
) -> RedirectResponse:
    """Begin an OAuth authorization flow.  Lit calls this on a Connect click.

    The ``user_id`` query param is the trust boundary: this endpoint *trusts*
    the caller (the Lit Python process) to assert which user is connecting.
    Lit gates its page on the Google OAuth session it already runs for
    every visitor, so the equivalent of a per-request bearer is the Google
    cookie on the originating browser.  If you wire a public client to this
    endpoint, add your own auth here.
    """
    srv = await _get_server_by_slug(server)
    if srv.auth_type != McpAuthType.OAUTH_OBO:
        raise HTTPException(status_code=400, detail=f"{server!r} is not OAUTH_OBO")

    cfg = srv.oauth_config or {}
    required = {"client_id", "authorize_url", "token_url"}
    if not required.issubset(cfg):
        raise HTTPException(
            status_code=500,
            detail=f"{server!r} oauth_config is missing one of: {sorted(required)}",
        )

    state = oauth_client.sign_state(user_id=user_id, server_slug=server, return_to=return_to)
    authorize = oauth_client.build_authorize_url(
        authorize_url=cfg["authorize_url"],
        client_id=cfg["client_id"],
        redirect_uri=_redirect_uri(request),
        scopes=cfg.get("scopes") or [],
        state=state,
        extra_params=cfg.get("extra_authorize_params"),
    )
    logger.info("MCP OAuth start", slug=server, user_id=user_id)
    return RedirectResponse(authorize, status_code=302)


@router.get("/callback")
async def oauth_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
) -> Any:
    """Exchange the provider's ``code`` for tokens, store the grant,
    redirect the user back to ``return_to``."""
    if error:
        return _result_page(
            ok=False,
            message=f"Provider returned error: {error} — {error_description or ''}",
            return_to="",
        )
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code or state")

    try:
        claims = oauth_client.verify_state(state)
    except ValueError as e:
        return _result_page(ok=False, message=f"Invalid state: {e}", return_to="")

    slug = claims["slug"]
    user_id = claims["sub"]
    return_to = claims.get("ret", "") or ""

    srv = await _get_server_by_slug(slug)
    cfg = srv.oauth_config or {}

    async with get_async_session() as session:
        secrets = await session.get(McpServerSecrets, srv.mcp_server_id)
        if not secrets or not secrets.oauth_client_secret_enc:
            return _result_page(ok=False, message=f"Server {slug!r} missing client_secret", return_to=return_to)
        client_secret = crypto.decrypt(secrets.oauth_client_secret_enc)
        if not client_secret:
            return _result_page(ok=False, message="client_secret decrypt failed", return_to=return_to)

        try:
            payload = await oauth_client.exchange_code(
                token_url=cfg["token_url"],
                client_id=cfg["client_id"],
                client_secret=client_secret,
                code=code,
                redirect_uri=_redirect_uri(request),
            )
        except Exception as e:
            logger.warning("MCP OAuth code exchange failed", slug=slug, user_id=user_id, error=str(e))
            return _result_page(ok=False, message=f"Token exchange failed: {e}", return_to=return_to)

        await grants.upsert_oauth_grant(
            session,
            user_id=user_id,
            mcp_server_id=srv.mcp_server_id,
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            token_type=payload.get("token_type", "Bearer"),
            expires_at=oauth_client.expiry_from_expires_in(payload.get("expires_in")),
            scopes=(payload.get("scope") or "").split() if payload.get("scope") else None,
        )
        await session.commit()

    if return_to:
        return RedirectResponse(return_to, status_code=302)
    return _result_page(ok=True, message=f"Connected to {srv.display_name}.", return_to="")


@router.post("/revoke")
async def revoke(
    server: str = Query(...),
    user_id: str = Query(...),
) -> dict[str, Any]:
    srv = await _get_server_by_slug(server)
    async with get_async_session() as session:
        ok = await grants.revoke_grant(session, user_id=user_id, mcp_server_id=srv.mcp_server_id)
        await session.commit()
    return {"revoked": ok}


class M2MSetBody(BaseModel):
    user_id: str
    api_key: str


@router.post("/m2m/set")
async def m2m_set(server: str, body: M2MSetBody) -> dict[str, Any]:
    """Accept a user-supplied API key for an M2M_PER_USER server."""
    srv = await _get_server_by_slug(server)
    if srv.auth_type != McpAuthType.M2M_PER_USER:
        raise HTTPException(status_code=400, detail=f"{server!r} is not M2M_PER_USER")
    async with get_async_session() as session:
        await grants.upsert_m2m_grant(
            session,
            user_id=body.user_id,
            mcp_server_id=srv.mcp_server_id,
            api_key=body.api_key,
        )
        await session.commit()
    return {"ok": True}


def _result_page(*, ok: bool, message: str, return_to: str) -> HTMLResponse:
    """Tiny HTML page shown when we don't have a ``return_to`` to redirect.

    Production deploys *should* always pass return_to; this is the fallback
    so a misconfigured Connect link still produces a readable result.
    """
    href = return_to or ""
    badge = "✓ Connected" if ok else "✗ Failed"
    color = "#1a7f37" if ok else "#cf222e"
    link = f'<p><a href="{href}">Back to Lit</a></p>' if href else ""
    qs = urlencode({"slug_result": "ok" if ok else "fail"})
    html = f"""<!doctype html><meta charset=utf-8>
<title>{badge}</title>
<style>body{{font:15px -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;color:#1f2328;
  max-width:560px;margin:60px auto;padding:0 24px}}
  h1{{color:{color}}}</style>
<h1>{badge}</h1><p>{message}</p>{link}
<p style="color:#57606a;font-size:12px">{qs}</p>"""
    return HTMLResponse(html)
