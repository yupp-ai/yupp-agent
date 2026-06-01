"""Thin sync HTTP client for the AHS artifact REST API.

We keep this purely synchronous because the CLI is a single-process
script and the simplicity of a top-to-bottom imperative ``push`` loop
outweighs any concurrency gain. The shape mirrors the routes in
``ypl/agent_harness_service/artifact_routes.py``.

The client raises :class:`AHSAPIError` on every non-2xx — callers decide
whether that means "error" or "expected miss" (e.g. ``404`` for an
unknown slug is part of the diff logic).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

# Match the per-artifact cap on the server so we never serialize a body
# we know will be rejected. Mirrors ``MAX_CONTENT_SIZE_BYTES`` in
# ``ypl/agent_harness_service/artifact_store.py``.
SERVER_MAX_CONTENT_BYTES = 10 * 1024 * 1024


@dataclass
class AHSAPIError(Exception):
    """A non-2xx response from AHS, normalized for the CLI."""

    status_code: int
    detail: str

    def __str__(self) -> str:
        return f"AHS {self.status_code}: {self.detail}"


@dataclass
class MemoryArtifact:
    """The MEMORY-relevant subset of an :class:`ArtifactResponse` row."""

    artifact_id: str
    slug: str
    version: int
    title: str
    description: str | None
    memory_scope: str | None
    memory_scope_subject: str | None
    created_at: str


class AHSMemoryClient:
    """Sync REST client scoped to MEMORY artifact operations.

    Constructed once per CLI invocation. The underlying :class:`httpx.Client`
    is exposed via context manager so a ``with`` block disposes of pooled
    connections cleanly.
    """

    def __init__(self, api_url: str, api_key: str, user_id: str | None, *, timeout: float = 30.0) -> None:
        headers = {"X-API-Key": api_key}
        if user_id:
            # The route layer reads ``X-User-ID`` to gate user-scope MEMORY
            # writes; we always set it so a malformed/missing flag produces
            # a clear 403 from the server rather than an opaque write to
            # the wrong subject.
            headers["X-User-ID"] = user_id
        self._http = httpx.Client(base_url=api_url.rstrip("/"), headers=headers, timeout=timeout)

    # Lifecycle ------------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> AHSMemoryClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # API methods ----------------------------------------------------------

    def list_user_memories(self, user_id: str, *, page_size: int = 200) -> list[MemoryArtifact]:
        """Return every (non-archived) MEMORY row under ``scope=user`` for ``user_id``.

        Pages through ``GET /ahs/artifacts`` until exhausted so callers
        always see the full set; the server caps ``limit`` at 200.
        """
        results: list[MemoryArtifact] = []
        offset = 0
        while True:
            params = {
                "type": "MEMORY",
                "scope": "user",
                "subject": user_id,
                "limit": page_size,
                "offset": offset,
            }
            rows = self._get_json("/ahs/artifacts", params=params).get("artifacts", [])
            if not rows:
                break
            for row in rows:
                slug = row.get("named_slug")
                if not slug:
                    # Defensive: every memory we care about has a slug.
                    continue
                results.append(
                    MemoryArtifact(
                        artifact_id=row["artifact_id"],
                        slug=slug,
                        version=row.get("version") or 0,
                        title=row.get("title") or "",
                        description=row.get("description"),
                        memory_scope=row.get("memory_scope"),
                        memory_scope_subject=row.get("memory_scope_subject"),
                        created_at=row.get("created_at") or "",
                    )
                )
            if len(rows) < page_size:
                break
            offset += len(rows)
        return results

    def get_memory_slug(self, slug: str, user_id: str) -> MemoryArtifact | None:
        """Return the latest version of ``slug`` for ``user_id``, or ``None`` on 404."""
        resp = self._http.get(
            f"/ahs/artifacts/by-slug/{slug}",
            params={"type": "MEMORY", "scope": "user", "subject": user_id},
        )
        if resp.status_code == 404:
            return None
        row = self._handle(resp)
        return MemoryArtifact(
            artifact_id=row["artifact_id"],
            slug=row.get("named_slug") or slug,
            version=row.get("version") or 0,
            title=row.get("title") or "",
            description=row.get("description"),
            memory_scope=row.get("memory_scope"),
            memory_scope_subject=row.get("memory_scope_subject"),
            created_at=row.get("created_at") or "",
        )

    def fetch_inline_content(self, artifact_id: str) -> str:
        """Fetch raw body bytes for an artifact id and decode as strict UTF-8.

        Used by ``--skip-unchanged`` and ``diff`` to compare server content
        against the local file. We decode strictly (symmetric with the
        strict ``Path.read_text(encoding="utf-8")`` on the local side):
        MEMORY rows are stored as ``str`` server-side, so valid UTF-8 is the
        contract. A row that fails to decode is a real corruption we surface
        as an :class:`AHSAPIError` rather than papering over with ``\\ufffd``
        replacement chars — callers already absorb ``AHSAPIError`` into an
        ``error`` outcome.
        """
        resp = self._http.get(f"/ahs/artifacts/{artifact_id}")
        if resp.status_code >= 400:
            self._raise_for(resp)
        try:
            return resp.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AHSAPIError(
                resp.status_code,
                f"artifact {artifact_id} body is not valid UTF-8: {exc}",
            ) from exc

    def create_memory_artifact(
        self,
        *,
        slug: str,
        inline_content: str,
        user_id: str,
        title: str,
        description: str | None = None,
        create_new_slug: bool = False,
    ) -> dict[str, Any]:
        """POST ``/ahs/artifacts`` to create a MEMORY row.

        With ``create_new_slug=False`` (default), the server appends a new
        version if the slug exists. If the slug is unknown, the server
        returns a 400 telling us to flip the flag — the ``push`` loop
        handles that retry so the caller doesn't have to.
        """
        body: dict[str, Any] = {
            "type": "MEMORY",
            "memory_scope": "user",
            "memory_scope_subject": user_id,
            "inline_content": inline_content,
            "content_type": "text/markdown",
            "title": title,
            "named_slug": slug,
            "create_new_slug": create_new_slug,
        }
        if description is not None:
            body["description"] = description
        resp = self._http.post("/ahs/artifacts", json=body)
        if resp.status_code >= 400:
            self._raise_for(resp)
        return resp.json()  # type: ignore[no-any-return]

    # Internals ------------------------------------------------------------

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._http.get(path, params=params)
        return self._handle(resp)

    def _handle(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code >= 400:
            self._raise_for(resp)
        return resp.json()  # type: ignore[no-any-return]

    @staticmethod
    def _raise_for(resp: httpx.Response) -> None:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise AHSAPIError(resp.status_code, str(detail))
