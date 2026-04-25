"""Artifact Viewer Starlette app.

Mount layout::

    GET /                                → home (search + recent)
    GET /search?q=...                    → search results
    GET /artifacts/{uuid}                → rendered artifact
    GET /artifacts/{uuid}/download       → download raw body as a file
    GET /artifacts/{uuid}/attachments/{filename} → stream attachment
    GET /artifacts/by-slug/{slug}        → latest version by slug
    GET /artifacts/by-slug/{slug}/v/{N}  → pinned version
    GET /artifacts/by-slug/{slug}/versions → version list

    GET /auth/{login,callback,logout,error}
    GET /healthz
    /static/*
"""

from __future__ import annotations

import logging
import os
import re
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

# Default page size for the listing page; matches the upstream AHS limit.
HOME_PAGE_SIZE = 20

# Type filter options shown on the home page. Matches AgentArtifactType enum
# values upstream — kept duplicated here so the viewer doesn't need to import
# the AHS Python package (it talks to AHS purely over HTTP).
_ARTIFACT_TYPE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("TEXT", "📝 text"),
    ("CODE_REVIEW", "🔍 code review"),
    ("OTHER", "📦 other"),
)
_VALID_ARTIFACT_TYPES = frozenset(value for value, _ in _ARTIFACT_TYPE_OPTIONS)

# Default ``type`` filter when the URL doesn't specify one. TEXT is by far
# the most common thing to browse; CODE_REVIEW entries are pointers to PRs
# and OTHER is rare, so leading with TEXT keeps the default view scoped to
# "documents the user actually wants to read". Pass ``type=`` (empty) to
# opt out and see every type.
_DEFAULT_ARTIFACT_TYPE = "TEXT"

# YYYY-MM-DD — what the date inputs in the filter row produce.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _logger() -> logging.Logger:
    return logging.getLogger("artifact_viewer.app")


def _normalize_date(raw: str | None, *, end_of_day: bool) -> str | None:
    """Convert a ``YYYY-MM-DD`` filter value into an ISO-8601 timestamp.

    The home page shows native ``<input type="date">`` controls, which post
    back a bare date. Converting to a full timestamp here keeps the date math
    on the server side: the "before" bound is exclusive and walks to the
    *next* midnight so the picker behaves as a date-inclusive filter.
    Returns ``None`` if the input doesn't match ``YYYY-MM-DD`` so a
    fat-fingered query param doesn't 422 the upstream call.
    """
    if not raw or not _DATE_RE.match(raw):
        return None
    if end_of_day:
        # Upstream uses `< created_before`, so add a day to make the picker
        # behave as a date-inclusive upper bound.
        from datetime import UTC, date, datetime, timedelta

        try:
            d = date.fromisoformat(raw)
        except ValueError:
            return None
        return datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=UTC).isoformat()
    return f"{raw}T00:00:00+00:00"


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
        # Stamped at OAuth callback time after AHS confirms the email maps to
        # a row in the users table. Used by the home page's "From me" filter
        # so we never have to round-trip back to AHS to resolve the user.
        "user_id": request.session.get("user_id", ""),
    }


async def home(request: Request) -> Response:
    """Recent artifacts page with a filter row and Prev/Next pagination.

    Filter / paging state lives in the URL so links are bookmarkable and the
    Prev/Next anchors round-trip every active filter. Default page size is
    :data:`HOME_PAGE_SIZE` (20); the upper bound matches the AHS endpoint's
    cap so a user can't request a giant page accidentally.

    Two filters have non-trivial defaults so the landing view is useful
    without the user touching the bar:

    * ``type`` defaults to ``TEXT`` (the most common, most readable type).
      Pass ``?type=`` (empty) to see every type.
    * ``from_me`` defaults to ``ON`` for signed-in users — the form posts a
      hidden ``from_me=0`` together with the checkbox so an unchecked box
      still sends a value, otherwise we couldn't tell "default" apart from
      "explicitly off".
    """
    user = _current_user(request)
    raw_type = request.query_params.get("type")
    if raw_type is None:
        # No ``type`` param at all — apply the default.
        artifact_type: str | None = _DEFAULT_ARTIFACT_TYPE
    else:
        # Empty string means the user explicitly chose "All". A bogus value
        # falls back to "no filter" rather than 400ing the upstream call.
        artifact_type = raw_type.strip().upper() or None
        if artifact_type and artifact_type not in _VALID_ARTIFACT_TYPES:
            artifact_type = None

    # Default-ON when signed in. If we have no user_id (rare — would mean
    # the session pre-dates the user_id stamping) we can't filter, so default
    # to OFF rather than send an empty creator_user_id and match nothing.
    raw_from_me = request.query_params.get("from_me")
    from_me = bool(user["user_id"]) if raw_from_me is None else raw_from_me.strip() == "1"
    creator_user_id = user["user_id"] if from_me and user["user_id"] else None
    creator_agent_id = (request.query_params.get("creator_agent_id") or "").strip() or None
    created_after = (request.query_params.get("created_after") or "").strip() or None
    created_before = (request.query_params.get("created_before") or "").strip() or None
    limit = _int_query(request, "limit", HOME_PAGE_SIZE, cap=200)
    if limit <= 0:
        limit = HOME_PAGE_SIZE
    offset = _int_query(request, "offset", 0)

    try:
        recent = await ahs_client.list_recent(
            limit=limit,
            offset=offset,
            artifact_type=artifact_type,
            creator_user_id=creator_user_id,
            creator_agent_id=creator_agent_id,
            created_after=_normalize_date(created_after, end_of_day=False),
            created_before=_normalize_date(created_before, end_of_day=True),
            include_total=True,
        )
    except AHSError as exc:
        return _error_page(request, exc, status_code=502)

    # Creator dropdowns: failures here shouldn't break the page — the filter
    # bar still renders (with raw-id text inputs) and the existing query
    # params remain in effect.
    creators: dict[str, Any] = {"users": [], "agents": []}
    try:
        creators = await ahs_client.list_creators()
    except AHSError as exc:
        _logger().warning("list_creators failed; rendering filter bar without dropdown options: %s", exc)

    artifacts = recent.get("artifacts", []) or []
    total = recent.get("total")
    has_next = (total is not None and offset + limit < total) or (total is None and len(artifacts) == limit)
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": user,
            "artifacts": artifacts,
            "query": "",
            "filters": {
                "type": artifact_type or "",
                "from_me": from_me,
                "creator_agent_id": creator_agent_id or "",
                "created_after": created_after or "",
                "created_before": created_before or "",
            },
            "creators": creators,
            "type_options": _ARTIFACT_TYPE_OPTIONS,
            "limit": limit,
            "offset": offset,
            "total": total,
            "page_size": HOME_PAGE_SIZE,
            "has_prev": offset > 0,
            "has_next": has_next,
            "prev_offset": max(0, offset - limit),
            "next_offset": offset + limit,
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


async def download(request: Request) -> Response:
    """Download an artifact's raw body as a file.

    Proxies the upstream AHS endpoint (which requires ``X-API-Key``) through
    the viewer's session auth so the browser never needs to talk to
    ``ahs.agcouch.com`` directly. The response is served as
    ``application/octet-stream`` with ``Content-Disposition: attachment`` so
    the browser saves it rather than rendering it — an HTML artifact can't
    execute script in the viewer's origin this way.
    """
    artifact_id = request.path_params["artifact_id"]
    try:
        data, content_type = await ahs_client.get_artifact_content(artifact_id)
    except AHSError as exc:
        return _error_page(request, exc)
    filename = _download_filename(artifact_id, content_type)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_CONTENT_TYPE_EXT = {
    "text/markdown": "md",
    "text/html": "html",
    "text/plain": "txt",
}


def _download_filename(artifact_id: str, content_type: str) -> str:
    """Build a safe ASCII filename for the Content-Disposition header.

    Uses the artifact id as the base (it's already URL-safe) and picks an
    extension from the content-type. Unknown types fall back to ``.txt``.
    """
    mime = content_type.split(";", 1)[0].strip().lower()
    ext = _CONTENT_TYPE_EXT.get(mime, "txt")
    return f"{artifact_id}.{ext}"


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
        Route("/artifacts/{artifact_id}/download", download, name="artifact_download"),
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
            secret_key=settings.VIEWER_SESSION_SECRET_KEY,
            max_age=settings.VIEWER_SESSION_MAX_AGE,
            same_site="lax",
            https_only=settings.VIEWER_SESSION_COOKIE_SECURE,
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
