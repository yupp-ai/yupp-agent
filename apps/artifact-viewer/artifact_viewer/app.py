"""Artifact Viewer Starlette app.

Mount layout::

    GET /                                → home (search + recent)
    GET /search?q=...                    → search results
    GET /artifacts/{uuid}                → rendered artifact
    GET /artifacts/{uuid}/attachments/{filename} → stream attachment
    GET /artifacts/by-slug/{slug}        → latest version by slug
    GET /artifacts/by-slug/{slug}/v/{N}  → pinned version
    GET /artifacts/by-slug/{slug}/versions → version list

    GET /auth/{login,callback,logout,error}
    GET /healthz
    /static/*
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from artifact_viewer import ahs_client, render
from artifact_viewer.ahs_client import AHSError
from artifact_viewer.auth import RequireLoginMiddleware, auth_routes
from artifact_viewer.config import settings
from artifact_viewer.templating import templates

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def healthz(_: Request) -> Response:
    return JSONResponse({"ok": True})


def _current_user(request: Request) -> dict[str, str]:
    return {
        "email": request.session.get("email", ""),
        "name": request.session.get("name", ""),
        "picture": request.session.get("picture", ""),
    }


async def home(request: Request) -> Response:
    try:
        recent = await ahs_client.list_recent(limit=20)
    except AHSError as exc:
        return _error_page(request, exc, status_code=502)
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": _current_user(request),
            "artifacts": recent.get("artifacts", []),
            "query": "",
        },
    )


async def search_page(request: Request) -> Response:
    query = (request.query_params.get("q") or "").strip()
    limit = _int_query(request, "limit", 50, cap=200)
    offset = _int_query(request, "offset", 0)
    results: list[dict[str, Any]] = []
    if query:
        try:
            data = await ahs_client.search(query, limit=limit, offset=offset)
        except AHSError as exc:
            return _error_page(request, exc, status_code=502)
        results = data.get("artifacts", [])
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "user": _current_user(request),
            "artifacts": results,
            "query": query,
            "limit": limit,
            "offset": offset,
        },
    )


async def artifact_by_id(request: Request) -> Response:
    artifact_id = request.path_params["artifact_id"]
    return await _render_artifact(request, artifact_id)


async def artifact_by_slug(request: Request) -> Response:
    slug = request.path_params["slug"]
    try:
        meta = await ahs_client.get_artifact_by_slug(slug)
    except AHSError as exc:
        return _error_page(request, exc)
    return await _render_artifact(request, meta["artifact_id"], meta=meta)


async def artifact_by_slug_version(request: Request) -> Response:
    slug = request.path_params["slug"]
    version = int(request.path_params["version"])
    try:
        meta = await ahs_client.get_artifact_by_slug(slug, version=version)
    except AHSError as exc:
        return _error_page(request, exc)
    return await _render_artifact(request, meta["artifact_id"], meta=meta)


async def versions_page(request: Request) -> Response:
    slug = request.path_params["slug"]
    try:
        data = await ahs_client.list_versions(slug)
    except AHSError as exc:
        return _error_page(request, exc)
    return templates.TemplateResponse(
        request,
        "versions.html",
        {
            "user": _current_user(request),
            "slug": slug,
            "versions": data.get("versions", []),
        },
    )


async def attachment(request: Request) -> Response:
    artifact_id = request.path_params["artifact_id"]
    filename = request.path_params["filename"]
    try:
        data, content_type = await ahs_client.get_attachment(artifact_id, filename)
    except AHSError as exc:
        return _error_page(request, exc)
    return Response(content=data, media_type=content_type)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _render_artifact(request: Request, artifact_id: str, *, meta: dict[str, Any] | None = None) -> Response:
    try:
        if meta is None:
            meta = await ahs_client.get_artifact_meta(artifact_id)
        content_bytes, content_type = await ahs_client.get_artifact_content(artifact_id)
    except AHSError as exc:
        return _error_page(request, exc)

    content_str = content_bytes.decode("utf-8", errors="replace")
    body_html, display_mode = render.render_artifact_body(content_str, content_type, artifact_id)
    attachments = (meta.get("metadata") or {}).get("attachments", []) or []
    attach_html = render.attachment_view_html(artifact_id, attachments)
    return templates.TemplateResponse(
        request,
        "artifact.html",
        {
            "user": _current_user(request),
            "meta": meta,
            "body_html": body_html,
            "display_mode": display_mode,
            "attachments_html": attach_html,
            "ahs_base_url": settings.AHS_BASE_URL,
        },
    )


def _int_query(request: Request, key: str, default: int, cap: int | None = None) -> int:
    raw = request.query_params.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < 0:
        return default
    if cap is not None:
        value = min(value, cap)
    return value


def _error_page(request: Request, exc: AHSError, status_code: int | None = None) -> Response:
    return templates.TemplateResponse(
        request,
        "upstream_error.html",
        {
            "user": _current_user(request),
            "status": exc.status_code,
            "detail": exc.detail,
        },
        status_code=status_code or (exc.status_code if 400 <= exc.status_code < 600 else 502),
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def build_app() -> Starlette:
    routes = [
        Route("/", home, name="home"),
        Route("/search", search_page, name="search"),
        Route("/artifacts/{artifact_id}", artifact_by_id, name="artifact"),
        Route(
            "/artifacts/{artifact_id}/attachments/{filename:path}",
            attachment,
            name="attachment",
        ),
        Route("/artifacts/by-slug/{slug}", artifact_by_slug, name="by_slug"),
        Route(
            "/artifacts/by-slug/{slug}/v/{version:int}",
            artifact_by_slug_version,
            name="by_slug_version",
        ),
        Route(
            "/artifacts/by-slug/{slug}/versions",
            versions_page,
            name="slug_versions",
        ),
        Route("/healthz", healthz, name="healthz"),
        *auth_routes,
        Mount("/static", app=StaticFiles(directory=str(_STATIC_DIR)), name="static"),
    ]

    middleware = [
        Middleware(
            SessionMiddleware,
            secret_key=settings.SESSION_SECRET_KEY,
            max_age=settings.SESSION_MAX_AGE,
            same_site="lax",
            https_only=settings.SESSION_COOKIE_SECURE,
        ),
        Middleware(RequireLoginMiddleware),
    ]

    return Starlette(routes=routes, middleware=middleware)


app = build_app()


def main() -> None:
    """Entry point for ``artifact-viewer`` console script."""
    import uvicorn

    uvicorn.run(
        "artifact_viewer.app:app",
        host=os.environ.get("VIEWER_HOST", settings.VIEWER_HOST),
        port=int(os.environ.get("VIEWER_PORT", settings.VIEWER_PORT)),
        workers=1,
        log_level="info",
    )


if __name__ == "__main__":
    main()
