"""Thin async client for the AHS artifact REST API.

The viewer is a stateless proxy: every request that needs artifact data
goes through here. The AHS shared secret never leaves this process.
"""

from __future__ import annotations

from typing import Any, cast

import httpx

from artifact_viewer.config import settings


class AHSError(Exception):
    """Raised when AHS returns a non-OK status or is unreachable."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"AHS {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _client(*, user_id: str | None = None) -> httpx.AsyncClient:
    """Build an authenticated client for AHS calls.

    ``user_id`` is forwarded as ``X-User-ID`` so AHS's MEMORY-write authz
    can verify the caller owns the (user-scope) memory they're editing.
    The header is omitted entirely when ``user_id`` is ``None`` so reads
    behave exactly as before.
    """
    headers = {"X-API-Key": settings.AGENT_HARNESS_SERVICE_API_KEY}
    if user_id:
        headers["X-User-ID"] = user_id
    return httpx.AsyncClient(
        base_url=settings.VIEWER_AHS_BASE_URL.rstrip("/"),
        headers=headers,
        timeout=30.0,
    )


async def _get_json(path: str, params: dict[str, Any] | None = None) -> Any:
    async with _client() as http:
        resp = await http.get(path, params=params)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise AHSError(resp.status_code, str(detail))
    return resp.json()


async def _get_bytes(path: str) -> tuple[bytes, str]:
    """Return (body, content_type) for a raw endpoint."""
    async with _client() as http:
        resp = await http.get(path)
    if resp.status_code >= 400:
        raise AHSError(resp.status_code, resp.text or "request failed")
    return resp.content, resp.headers.get("content-type", "application/octet-stream")


# ---------------------------------------------------------------------------
# Metadata endpoints
# ---------------------------------------------------------------------------


async def get_artifact_meta(artifact_id: str) -> dict[str, Any]:
    return cast(dict[str, Any], await _get_json(f"/ahs/artifacts/{artifact_id}/meta"))


async def get_artifact_by_slug(slug: str, version: int | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if version is not None:
        params["version"] = version
    return cast(dict[str, Any], await _get_json(f"/ahs/artifacts/by-slug/{slug}", params=params or None))


async def list_versions(
    slug: str,
    *,
    artifact_type: str | None = None,
    scope: str | None = None,
    subject: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """List all versions of ``slug``.

    For MEMORY slugs the upstream route requires a ``scope`` query param
    (and ``subject`` for user/agent scopes); ``user_id`` is forwarded as
    ``X-User-ID`` so the upstream's read-authz check passes when looking
    up a user-scoped slug owned by the caller.
    """
    params: dict[str, Any] = {}
    if artifact_type:
        params["type"] = artifact_type
    if scope:
        params["scope"] = scope
    if subject:
        params["subject"] = subject
    async with _client(user_id=user_id) as http:
        resp = await http.get(f"/ahs/artifacts/by-slug/{slug}/versions", params=params or None)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise AHSError(resp.status_code, str(detail))
    return cast(dict[str, Any], resp.json())


async def list_recent(
    limit: int = 20,
    offset: int = 0,
    *,
    artifact_type: str | None = None,
    creator_user_id: str | None = None,
    creator_agent_id: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    include_total: bool = False,
) -> dict[str, Any]:
    """Fetch artifacts via ``GET /ahs/artifacts``.

    Empty / None filter values are not forwarded to AHS so the upstream
    handler treats them as "no filter applied".
    """
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if artifact_type:
        # Upstream uses query name ``type`` (aliased on the FastAPI handler).
        params["type"] = artifact_type
    if creator_user_id:
        params["creator_user_id"] = creator_user_id
    if creator_agent_id:
        params["creator_agent_id"] = creator_agent_id
    if created_after:
        params["created_after"] = created_after
    if created_before:
        params["created_before"] = created_before
    if include_total:
        params["include_total"] = "true"
    return cast(dict[str, Any], await _get_json("/ahs/artifacts", params=params))


async def search(query: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        await _get_json("/ahs/artifacts/search", params={"q": query, "limit": limit, "offset": offset}),
    )


async def list_creators() -> dict[str, Any]:
    """Fetch distinct creators (users + agents) for filter dropdowns."""
    return cast(dict[str, Any], await _get_json("/ahs/artifacts/creators"))


# ---------------------------------------------------------------------------
# Raw content endpoints
# ---------------------------------------------------------------------------


async def get_artifact_content(artifact_id: str) -> tuple[bytes, str]:
    return await _get_bytes(f"/ahs/artifacts/{artifact_id}")


async def get_attachment(artifact_id: str, filename: str) -> tuple[bytes, str]:
    return await _get_bytes(f"/ahs/artifacts/{artifact_id}/attachments/{filename}")


# ---------------------------------------------------------------------------
# Mutation: create a new version of an existing artifact
# ---------------------------------------------------------------------------


async def create_new_version(
    *,
    artifact_type: str,
    title: str,
    description: str | None,
    named_slug: str,
    content_type: str,
    content: str | None = None,
    inline_content: str | None = None,
    memory_scope: str | None = None,
    memory_scope_subject: str | None = None,
    creator_user_id: str | None = None,
) -> dict[str, Any]:
    """Create a new version of an existing slugged artifact.

    Wraps ``POST /ahs/artifacts`` with ``create_new_slug=False``: AHS
    looks up the slug's max version and inserts a row at ``max+1``,
    regardless of which version the caller was viewing when they
    clicked Edit. Editing v3 of a slug with v1..v4 produces v5.

    For TEXT artifacts pass ``content`` (the new body) and
    ``content_type``. For MEMORY artifacts pass ``inline_content`` plus
    the (scope, subject) tuple — AHS enforces write authorization on
    those using the ``X-User-ID`` header forwarded by ``_client``.

    Returns the newly-created artifact's metadata (matches the
    ``CreateArtifactResponse`` shape — includes ``artifact_id``,
    ``version``, ``slug_url`` and the rest).
    """
    body: dict[str, Any] = {
        "type": artifact_type,
        "title": title,
        "description": description,
        "content_type": content_type,
        "named_slug": named_slug,
        "create_new_slug": False,
        "creator_user_id": creator_user_id,
    }
    if content is not None:
        body["content"] = content
    if inline_content is not None:
        body["inline_content"] = inline_content
    if memory_scope is not None:
        body["memory_scope"] = memory_scope
    if memory_scope_subject is not None:
        body["memory_scope_subject"] = memory_scope_subject

    async with _client(user_id=creator_user_id) as http:
        resp = await http.post("/ahs/artifacts", json=body)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise AHSError(resp.status_code, str(detail))
    return cast(dict[str, Any], resp.json())


# ---------------------------------------------------------------------------
# User resolution (membership check)
# ---------------------------------------------------------------------------


async def resolve_user(email: str) -> str | None:
    """Ask AHS whether ``email`` is a known user.

    Returns the ``user_id`` on success. Returns ``None`` for 404 (the
    email isn't in the users table) so callers can convert it into an
    access-denied page. Any other upstream error bubbles up as
    :class:`AHSError`.
    """
    async with _client() as http:
        resp = await http.post("/ahs/resolve_user", json={"email": email})
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise AHSError(resp.status_code, str(detail))
    data = resp.json()
    user_id = data.get("user_id")
    return str(user_id) if user_id else None
