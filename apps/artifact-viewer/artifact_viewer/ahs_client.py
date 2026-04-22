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


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.VIEWER_AHS_BASE_URL.rstrip("/"),
        headers={"X-API-Key": settings.AGENT_HARNESS_SERVICE_API_KEY},
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


async def list_versions(slug: str) -> dict[str, Any]:
    return cast(dict[str, Any], await _get_json(f"/ahs/artifacts/by-slug/{slug}/versions"))


async def list_recent(limit: int = 20, offset: int = 0) -> dict[str, Any]:
    return cast(dict[str, Any], await _get_json("/ahs/artifacts", params={"limit": limit, "offset": offset}))


async def search(query: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        await _get_json("/ahs/artifacts/search", params={"q": query, "limit": limit, "offset": offset}),
    )


# ---------------------------------------------------------------------------
# Raw content endpoints
# ---------------------------------------------------------------------------


async def get_artifact_content(artifact_id: str) -> tuple[bytes, str]:
    return await _get_bytes(f"/ahs/artifacts/{artifact_id}")


async def get_attachment(artifact_id: str, filename: str) -> tuple[bytes, str]:
    return await _get_bytes(f"/ahs/artifacts/{artifact_id}/attachments/{filename}")


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
