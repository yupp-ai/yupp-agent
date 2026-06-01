"""Artifact Viewer Starlette app.

Mount layout::

    GET /                                → home (search + recent)
    GET /search?q=...                    → search results (supports label: / slug: tokens)
    GET /a/{uuid}              → rendered artifact (a = by id)
    GET /a/{uuid}/raw          → full-page sandboxed HTML (text/html only)
    GET /a/{uuid}/download     → download raw body as a file
    GET /a/{uuid}/edit         → full-screen edit form (creates a new version on POST)
    POST /a/{uuid}/edit        → submit new content; redirects to the new version
    GET /a/{uuid}/attachments/{filename} → stream attachment
    GET /s/{slug}              → latest version by slug (s = by slug)
    GET /s/{slug}/{N}          → pinned version

    A slug's full history is a ``slug:{slug}`` search — there is no dedicated
    versions page. Legacy /artifacts/{uuid} and /artifacts/by-slug/... paths
    still resolve (the old /versions URL 303-redirects to the slug: search).

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
from urllib.parse import quote

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from artifact_viewer import ahs_client, render
from artifact_viewer.ahs_client import AHSError
from artifact_viewer.auth import RequireLoginMiddleware, auth_routes
from artifact_viewer.config import settings
from artifact_viewer.templating import templates

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Cache-busting token for the stylesheet, derived from style.css's mtime so a
# redeploy (which rewrites the file) forces browsers to refetch instead of
# serving a stale cached CSS. Exposed to every template as ``static_version``.
try:
    _STATIC_VERSION = str(int((_STATIC_DIR / "style.css").stat().st_mtime))
except OSError:
    _STATIC_VERSION = "0"
templates.env.globals["static_version"] = _STATIC_VERSION

# Default page size for the listing page. The upstream AHS endpoint caps
# results at 200, so 50 keeps a comfortable margin while showing enough
# recent work to scan without paging.
HOME_PAGE_SIZE = 50

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
_LABEL_TOKEN_RE = re.compile(r"(?:^|\s)label:([^\s]+)", re.IGNORECASE)


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


def _normalize_labels(labels: list[str] | str | None) -> list[str]:
    if labels is None:
        return []
    raw_labels = [part.strip() for part in labels.split(",")] if isinstance(labels, str) else labels
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_labels:
        label = raw.strip().removeprefix("#").lower()
        if not label or not re.match(r"^[a-z0-9][a-z0-9._-]{0,62}$", label):
            continue
        if label not in seen:
            normalized.append(label)
            seen.add(label)
    return normalized


def _extract_label_terms(query: str, explicit: list[str] | None = None) -> tuple[str, list[str]]:
    labels = list(explicit or [])
    labels.extend(match.group(1) for match in _LABEL_TOKEN_RE.finditer(query))
    text_query = _LABEL_TOKEN_RE.sub(" ", query)
    text_query = re.sub(r"\s+", " ", text_query).strip()
    return text_query, _normalize_labels(labels)


def _full_page_headers() -> dict[str, str]:
    """Response headers that pin a full-page artifact to the same sandboxed
    posture as the in-page iframe.

    Shared by the ``/raw`` route and the ``?v=full`` query-param alias so the
    two URL styles are byte-for-byte identical in protection: a CSP ``sandbox``
    directive (no ``allow-scripts`` / ``allow-same-origin``) gives the
    top-level document an opaque origin with no JS and no access to the
    viewer's cookies, plus defense-in-depth framing / sniffing / referrer /
    caching headers. See ``raw_html`` for the full rationale.
    """
    return {
        "Content-Security-Policy": f"sandbox {render.HTML_SANDBOX_FLAGS}; frame-ancestors 'none'",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "private, no-store",
    }


def _full_page_response(artifact_id: str, content_bytes: bytes, content_str: str, content_type: str) -> Response:
    """Build the sandboxed full-page response for an HTML or markdown artifact.

    HTML is served as-is (already the raw agent-authored body); markdown is
    rendered to the same bleach-sanitized fragment as the normal view, wrapped
    in a minimal chrome-free shell. Both carry the identical protective headers
    from :func:`_full_page_headers`, so ``?v=full`` is exactly ``/raw`` with a
    nicer URL.
    """
    if content_type.split(";", 1)[0].strip().lower() == "text/html":
        return Response(content=content_bytes, media_type="text/html; charset=utf-8", headers=_full_page_headers())
    body_html, display_mode = render.render_artifact_body(content_str, content_type, artifact_id)
    page = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<link rel='stylesheet' href='/static/style.css'>"
        "<title></title></head><body class='full-artifact-body'>"
        f"<main class='artifact-body artifact-body-{display_mode}'>{body_html}</main>"
        "</body></html>"
    )
    return Response(content=page, media_type="text/html; charset=utf-8", headers=_full_page_headers())


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
    :data:`HOME_PAGE_SIZE` (50); the upper bound matches the AHS endpoint's
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
    # Default OFF: the landing page shows ALL docs (not just the viewer's own).
    # "From me" is an opt-in filter the user ticks; it only engages when the
    # query param is explicitly "1".
    raw_from_me = request.query_params.get("from_me")
    from_me = raw_from_me is not None and raw_from_me.strip() == "1"
    creator_user_id = user["user_id"] if from_me and user["user_id"] else None
    explicit_creator_user_id = (request.query_params.get("creator_user_id") or "").strip() or None
    if explicit_creator_user_id:
        creator_user_id = explicit_creator_user_id
        from_me = bool(user["user_id"] and explicit_creator_user_id == user["user_id"])
    creator_agent_id = (request.query_params.get("creator_agent_id") or "").strip() or None
    created_after = (request.query_params.get("created_after") or "").strip() or None
    created_before = (request.query_params.get("created_before") or "").strip() or None
    query = (request.query_params.get("q") or "").strip()
    text_query, label_terms = _extract_label_terms(query)
    limit = _int_query(request, "limit", HOME_PAGE_SIZE, cap=200)
    if limit <= 0:
        limit = HOME_PAGE_SIZE
    offset = _int_query(request, "offset", 0)

    try:
        if query:
            recent = await ahs_client.search(query, limit=limit, offset=offset, latest_per_slug=True)
        else:
            recent = await ahs_client.list_recent(
                limit=limit,
                offset=offset,
                artifact_type=artifact_type,
                creator_user_id=creator_user_id,
                creator_agent_id=creator_agent_id,
                created_after=_normalize_date(created_after, end_of_day=False),
                created_before=_normalize_date(created_before, end_of_day=True),
                labels=label_terms,
                latest_per_slug=True,
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
            "query": query,
            "label_terms": label_terms,
            "text_query": text_query,
            "filters": {
                "type": artifact_type or "",
                "from_me": from_me,
                "creator_user_id": explicit_creator_user_id or "",
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
            data = await ahs_client.search(query, limit=limit, offset=offset, latest_per_slug=True)
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


async def update_labels(request: Request) -> Response:
    artifact_id = request.path_params["artifact_id"]
    user = _current_user(request)
    form = await request.form()
    raw_labels = form.get("labels")
    labels = _normalize_labels(raw_labels if isinstance(raw_labels, str) else "")
    try:
        meta = await ahs_client.update_labels(
            artifact_id=artifact_id,
            labels=labels,
            user_id=user.get("user_id") or None,
        )
    except AHSError as exc:
        return _error_page(request, exc)
    return JSONResponse({"ok": True, "labels": meta.get("labels", [])})


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


async def slug_versions_redirect(request: Request) -> Response:
    """Legacy ``/artifacts/by-slug/{slug}/versions`` → a ``slug:`` search.

    The dedicated versions page was retired: a slug's full history is now just
    a search-result listing for ``slug:{slug}`` (the AHS search route returns
    every version, newest first, when a ``slug:`` token is present). Clicking a
    slug anywhere in the UI lands here.
    """
    slug = request.path_params["slug"]
    return RedirectResponse(f"/search?q=slug:{quote(slug, safe='')}", status_code=303)


async def attachment(request: Request) -> Response:
    artifact_id = request.path_params["artifact_id"]
    filename = request.path_params["filename"]
    try:
        data, content_type = await ahs_client.get_attachment(artifact_id, filename)
    except AHSError as exc:
        return _error_page(request, exc)
    return Response(content=data, media_type=content_type)


async def raw_html(request: Request) -> Response:
    """Serve an HTML artifact's raw body in a sandboxed full-page view.

    Linked from the "Full Page" button on artifact pages. Only ``text/html``
    artifacts are served here (everything else 404s) — the use case is "let
    me see the agent's HTML in the full browser viewport without the
    iframe's scrollbars and fixed dimensions".

    Security model: even though the URL lives on the viewer's origin
    (``artifacts.agcouch.com``), the response carries a
    ``Content-Security-Policy: sandbox`` header with the same flags as the
    in-page iframe (no ``allow-scripts``, no ``allow-same-origin``). The
    browser treats the top-level document as if it were in a sandboxed
    iframe: opaque origin, no JS execution, ``document.cookie`` empty,
    same-origin ``fetch`` becomes cross-origin and is CORS-blocked. This
    keeps parity with the iframe's posture so an agent-authored page can't
    exfiltrate the viewer's session, regardless of whether it renders
    in-page or full-page.

    A separate ``artifacts-content.agcouch.com`` origin would be a stronger
    second layer (and is recommended as future hardening), but the CSP
    sandbox header alone is sufficient to neutralise the same-origin XSS
    surface.
    """
    artifact_id = request.path_params["artifact_id"]
    try:
        data, content_type = await ahs_client.get_artifact_content(artifact_id)
    except AHSError as exc:
        return _error_page(request, exc)
    mime = content_type.split(";", 1)[0].strip().lower() if content_type else ""
    if mime != "text/html":
        # The "Full Page" button only appears for HTML artifacts, so this
        # branch is only reachable if someone hand-edits the URL or clicks
        # a stale link. Redirect (in the new tab) back to the normal
        # rendered page rather than showing the viewer's error template —
        # the user's original tab still has the artifact open and a 404
        # in the new tab would be confusing UX with no extra signal.
        return RedirectResponse(f"/a/{artifact_id}", status_code=303)
    # Same sandboxed posture as the ``?v=full`` alias — see ``_full_page_headers``.
    return Response(content=data, media_type="text/html; charset=utf-8", headers=_full_page_headers())


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


async def edit_artifact_get(request: Request) -> Response:
    """Show a full-screen edit form pre-filled with the raw artifact body.

    Saves are always "fork the version chain at the top": even when the
    user is editing v3 of a v1..v4 slug, submitting creates v5 (max + 1).
    The template surfaces this prominently so nobody mistakes the edit
    for an in-place mutation. We refuse to render the form for artifacts
    the caller can't edit (no slug, or MEMORY scope/subject they don't
    own) so a stray ``/edit`` URL in the wild can't bypass the gating.
    """
    artifact_id = request.path_params["artifact_id"]
    user = _current_user(request)
    try:
        meta = await ahs_client.get_artifact_meta(artifact_id)
        content_bytes, _content_type = await ahs_client.get_artifact_content(artifact_id)
    except AHSError as exc:
        return _error_page(request, exc)

    if not _can_edit(meta, user):
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "user": user,
                "reason": "not_editable",
                "email": user.get("email", ""),
            },
            status_code=403,
        )

    raw_text = content_bytes.decode("utf-8", errors="replace")
    next_version = await _resolve_next_version(meta, user)
    return templates.TemplateResponse(
        request,
        "edit.html",
        {
            "user": user,
            "meta": meta,
            "raw_text": raw_text,
            "next_version": next_version,
        },
    )


async def edit_artifact_post(request: Request) -> Response:
    """Persist edits as a brand-new artifact version.

    Calls AHS ``POST /ahs/artifacts`` with ``create_new_slug=False`` —
    AHS computes ``next_version = max(version) + 1`` server-side and
    inserts the row, so two simultaneous edits can't collide on the same
    integer (the unique key on ``(slug, version)`` would reject the
    second). On success we redirect to the new version's canonical URL
    so a refresh-after-save behaves predictably.
    """
    artifact_id = request.path_params["artifact_id"]
    user = _current_user(request)
    try:
        meta = await ahs_client.get_artifact_meta(artifact_id)
    except AHSError as exc:
        return _error_page(request, exc)

    if not _can_edit(meta, user):
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "user": user,
                "reason": "not_editable",
                "email": user.get("email", ""),
            },
            status_code=403,
        )

    form = await request.form()
    raw_text_value = form.get("content")
    new_text = raw_text_value if isinstance(raw_text_value, str) else ""

    # Empty / whitespace-only submissions: refuse to create a blank
    # successor. AHS only rejects ``None`` (not empty strings), so without
    # this guard a user who clears the textarea and clicks Save would
    # silently wipe the latest visible version. Re-render the form with
    # the raw text the user typed so they don't lose what they were
    # working on (which, in this case, is exactly the empty string).
    if not new_text.strip():
        next_version = await _resolve_next_version(meta, user)
        return templates.TemplateResponse(
            request,
            "edit.html",
            {
                "user": user,
                "meta": meta,
                "raw_text": new_text,
                "next_version": next_version,
                "submit_error": (
                    "Refusing to save an empty body. Type something, or click Cancel "
                    "to leave the existing version untouched."
                ),
            },
            status_code=400,
        )

    artifact_type = (meta.get("type") or "TEXT").upper()
    is_memory = artifact_type == "MEMORY"

    # Provenance: record which version this edit was made from and who
    # the original creator was. Carrying the original creator forward
    # (chained via metadata.original_creator_user_id when the source row
    # was itself an edit) keeps an authorship trail in the artifact
    # registry even though the new row's ``creator_user_id`` is the
    # editor — without this, the only audit signal would be the version
    # chain itself.
    source_metadata = meta.get("metadata") or {}
    original_creator = source_metadata.get("original_creator_user_id") or meta.get("creator_user_id")
    edit_metadata: dict[str, Any] = {
        "edited_from_artifact_id": str(meta.get("artifact_id") or artifact_id),
        "edited_from_version": meta.get("version"),
        "edited_via": "artifact-viewer",
    }
    if original_creator:
        edit_metadata["original_creator_user_id"] = original_creator

    try:
        created = await ahs_client.create_new_version(
            artifact_type=artifact_type,
            title=str(meta.get("title") or ""),
            description=meta.get("description"),
            named_slug=str(meta["named_slug"]),
            content_type=str(meta.get("content_type") or "text/markdown"),
            content=None if is_memory else new_text,
            inline_content=new_text if is_memory else None,
            memory_scope=meta.get("memory_scope") if is_memory else None,
            memory_scope_subject=meta.get("memory_scope_subject") if is_memory else None,
            creator_user_id=user.get("user_id") or None,
            extra_metadata=edit_metadata,
            labels=meta.get("labels") or [],
        )
    except AHSError as exc:
        return _error_page(request, exc)

    # Redirect to the new version. For non-MEMORY artifacts we use the
    # slug-pinned URL — that's the human-friendly link the version page
    # also points to. For MEMORY we fall back to the by-id URL: the
    # slug route's ``read_artifact_by_slug_route`` handler defaults the
    # ``type`` query param to TEXT and the viewer's ``ahs_client`` doesn't
    # forward ``type=MEMORY`` / ``scope`` / ``subject`` / ``X-User-ID``,
    # so a slug-redirect would 404 on a successful MEMORY save and look
    # like the edit failed. The by-id route doesn't share that pitfall.
    new_id = created.get("artifact_id") or artifact_id
    if is_memory:
        return RedirectResponse(f"/a/{new_id}", status_code=303)
    new_slug = created.get("named_slug") or meta.get("named_slug")
    new_version = created.get("version")
    if new_slug and new_version is not None:
        return RedirectResponse(f"/s/{new_slug}/{new_version}", status_code=303)
    # Fallback: by-id. Should be unreachable for slugged artifacts (which
    # is the only kind we allow to edit) but keeps the response well-formed
    # if AHS ever omits the version field.
    return RedirectResponse(f"/a/{new_id}", status_code=303)


async def _resolve_next_version(meta: dict[str, Any], user: dict[str, str]) -> int | None:
    """Best-effort prediction of the next-version number for the edit banner.

    Looks at all visible versions of the slug and returns ``max + 1`` so
    the form can render a concrete "Save as v{N}" CTA. Returns ``None``
    when we *can't* compute a reliable answer (no slug, AHS unreachable,
    or transport-level failure). The template falls back to a generic
    "next version (server-assigned)" message in that case rather than a
    misleading ``current + 1`` — which can be off by many on a slug
    where the user is editing an old version.

    The actual version assignment always happens server-side at submit
    time (``create_new_slug=False`` lets AHS resolve ``max + 1``), so a
    stale or absent banner is purely cosmetic and the redirect after
    save still lands on the correct, real version.
    """
    slug = meta.get("named_slug")
    if not slug:
        return None
    artifact_type = (meta.get("type") or "TEXT").upper()
    scope: str | None = None
    subject: str | None = None
    type_param: str | None = None
    if artifact_type == "MEMORY":
        type_param = "MEMORY"
        scope = meta.get("memory_scope")
        subject = meta.get("memory_scope_subject")
    try:
        data = await ahs_client.list_versions(
            str(slug),
            artifact_type=type_param,
            scope=scope,
            subject=subject,
            user_id=user.get("user_id") or None,
        )
    except (AHSError, httpx.HTTPError) as exc:
        # ``AHSError`` covers HTTP-level failures (status >= 400);
        # ``httpx.HTTPError`` is the umbrella for transport-level issues
        # (ConnectError, ReadTimeout, RemoteProtocolError, ...). Without
        # the broader catch, a transient AHS hiccup would 500 the entire
        # edit GET page even though this is documented as best-effort.
        _logger().warning("list_versions failed while building edit banner for slug=%r: %s", slug, exc)
        return None
    versions = data.get("versions") or []
    if not versions:
        return None
    max_version = max(int(v.get("version") or 0) for v in versions)
    return max_version + 1


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
    if (request.query_params.get("v") or "").lower() == "full":
        return _full_page_response(artifact_id, content_bytes, content_str, content_type)
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
            "can_edit": _can_edit(meta, _current_user(request)),
            "labels": meta.get("labels") or [],
        },
    )


# Content-types the editor's plain-text textarea can faithfully round-trip.
# HTML is intentionally excluded — the textarea isn't a structured editor and
# typing markdown/plain into a slug whose content_type=text/html silently
# re-stamps the content as HTML, which renders as broken layout in the
# sandboxed iframe. Restricting at the gate is a cleaner UX than offering an
# Edit button that mangles your edit on save.
_EDITABLE_CONTENT_TYPES: frozenset[str] = frozenset({"text/markdown", "text/plain"})


def _can_edit(meta: dict[str, Any], user: dict[str, str]) -> bool:
    """Decide whether the signed-in user is allowed to start an edit.

    Editing always means "create a new version", which only makes sense
    for slugged artifacts of an editable content_type. We restrict by:

    * **Type** — TEXT and MEMORY only. CODE_REVIEW and OTHER are
      pointer-only artifacts (no body) so the editor would have nothing
      to load; un-slugged anything has no version chain to extend.
    * **Content-type** (TEXT) — only ``text/markdown`` and
      ``text/plain``. Editing an ``text/html`` artifact in a plain
      textarea would re-stamp the content as HTML and render through
      the sandboxed iframe, breaking markdown formatting.
    * **MEMORY scope** — same rules AHS applies on write
      (``caller_can_write_memory``):

      - ``topic`` — anyone signed in can edit (open by design).
      - ``user`` — only the user matching ``memory_scope_subject``.
      - ``agent`` — never editable from the viewer; agents write
        their own memories and a human shouldn't impersonate them.
        Hide the button rather than show one that always 403s.

    Archived rows are still editable: editing produces a fresh active
    version, which is a perfectly reasonable way to "un-archive" by
    rewriting.

    The TEXT branch deliberately keeps the "any signed-in user can
    edit" policy — viewer membership is already gated to the AHS
    users table, so this is a Google-Docs-style team workspace, not a
    public surface. We persist the *original* creator on the new
    version's metadata (see ``edit_artifact_post``) so authorship
    history stays auditable.
    """
    if not meta.get("named_slug"):
        return False
    artifact_type = (meta.get("type") or "").upper()
    if artifact_type == "MEMORY":
        scope = meta.get("memory_scope")
        subject = meta.get("memory_scope_subject")
        if scope == "topic":
            return True
        if scope == "user":
            return bool(user.get("user_id")) and subject == user.get("user_id")
        # Includes scope == "agent" and any unexpected value.
        return False
    if artifact_type != "TEXT":
        # CODE_REVIEW / OTHER / any future pointer-only types: no body to edit.
        return False
    content_type = (meta.get("content_type") or "").split(";", 1)[0].strip().lower()
    return content_type in _EDITABLE_CONTENT_TYPES


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
        # --- Canonical short URLs: /a/{uuid} for a specific row, /s/{slug}
        #     for a slug (latest) and /s/{slug}/{version} for a pinned version. ---
        Route("/a/{artifact_id}", artifact_by_id, name="artifact"),
        Route("/a/{artifact_id}/raw", raw_html, name="artifact_raw"),
        Route("/a/{artifact_id}/download", download, name="artifact_download"),
        Route("/a/{artifact_id}/labels", update_labels, methods=["POST"], name="artifact_labels"),
        Route("/a/{artifact_id}/edit", edit_artifact_get, methods=["GET"], name="artifact_edit"),
        Route("/a/{artifact_id}/edit", edit_artifact_post, methods=["POST"], name="artifact_edit_submit"),
        Route("/a/{artifact_id}/attachments/{filename:path}", attachment, name="attachment"),
        Route("/s/{slug}", artifact_by_slug, name="by_slug"),
        Route("/s/{slug}/{version:int}", artifact_by_slug_version, name="by_slug_version"),
        # --- Legacy paths (pre-2026-06 scheme). Kept so stored ``url`` fields,
        #     rendered attachment links, and external bookmarks keep resolving.
        #     The old /versions page is gone — it now redirects to a slug: search. ---
        Route("/artifacts/by-slug/{slug}/versions", slug_versions_redirect, name="slug_versions"),
        Route("/artifacts/by-slug/{slug}/v/{version:int}", artifact_by_slug_version, name="by_slug_version_legacy"),
        Route("/artifacts/by-slug/{slug}", artifact_by_slug, name="by_slug_legacy"),
        Route("/artifacts/{artifact_id}/raw", raw_html, name="artifact_raw_legacy"),
        Route("/artifacts/{artifact_id}/download", download, name="artifact_download_legacy"),
        Route("/artifacts/{artifact_id}/labels", update_labels, methods=["POST"], name="artifact_labels_legacy"),
        Route("/artifacts/{artifact_id}/edit", edit_artifact_get, methods=["GET"], name="artifact_edit_legacy"),
        Route("/artifacts/{artifact_id}/edit", edit_artifact_post, methods=["POST"], name="artifact_edit_post_legacy"),
        Route("/artifacts/{artifact_id}/attachments/{filename:path}", attachment, name="attachment_legacy"),
        Route("/artifacts/{artifact_id}", artifact_by_id, name="artifact_legacy"),
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
