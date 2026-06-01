"""Push + diff orchestration. Keeps argparse out of the way of testing.

Three primitives here:

- :class:`PushOutcome`     — per-file result of one ``push`` operation.
- :func:`push_workspace`   — drive the upload loop and emit a list of outcomes.
- :func:`diff_workspace`   — 3-way compare local walk vs. server list.

All HTTP I/O routes through :class:`ahs_memory.client.AHSMemoryClient`
so the test suite can patch a respx ``MockTransport`` and exercise the
loop deterministically.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ahs_memory.client import AHSAPIError, MemoryArtifact
from ahs_memory.walker import Candidate


class MemoryClientProtocol(Protocol):
    """Structural subset of :class:`ahs_memory.client.AHSMemoryClient`.

    Declared as a Protocol so the test suite's hand-rolled fake can be
    passed in without inheriting from the real class. Keeps the pusher
    decoupled from the HTTP layer and makes the surface explicit — adding
    a method here forces a matching test-double update.
    """

    def list_user_memories(self, user_id: str, *, page_size: int = ...) -> list[MemoryArtifact]: ...

    def get_memory_slug(self, slug: str, user_id: str) -> MemoryArtifact | None: ...

    def fetch_inline_content(self, artifact_id: str) -> str: ...

    def create_memory_artifact(
        self,
        *,
        slug: str,
        inline_content: str,
        user_id: str,
        title: str,
        description: str | None = ...,
        create_new_slug: bool = ...,
    ) -> dict[str, Any]: ...


PushStatus = Literal[
    "created",
    "new-version",
    "skipped-unchanged",
    "skipped-oversize",
    "skipped-unsafe",
    "error",
]
"""Allowed outcome statuses surfaced in the final ``push`` report."""

DiffStatus = Literal["local-only", "remote-only", "changed", "unchanged"]
"""Allowed statuses for one row in a 3-way diff between local + remote."""

# Substring the server embeds in its "append to an unknown slug" 400. We key
# the create-vs-append retry below off it. Mirrors ``SLUG_UNKNOWN_HINT`` in
# ``ypl/agent_harness_service/artifact_store.py``; that module has a regression
# test pinning the wording, so this duplicated literal can't silently drift.
_SLUG_UNKNOWN_HINT = "does not exist yet"


@dataclass(frozen=True)
class PushOutcome:
    """Per-file result of one ``push`` call."""

    rel_path: str
    slug: str
    status: PushStatus
    detail: str = ""
    # Populated on success; ``0`` on skips/errors.
    version: int = 0
    size_bytes: int = 0


@dataclass(frozen=True)
class DiffRow:
    """One row of a workspace ↔ AHS diff."""

    slug: str
    status: DiffStatus
    # Empty when the row is ``remote-only``.
    rel_path: str = ""
    # Empty when the row is ``local-only`` or the server slug was missing.
    artifact_id: str = ""
    # Local-side size when known.
    size_bytes: int = 0


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _title_from(rel_path: str) -> str:
    """Default artifact title for a markdown file.

    The artifact viewer renders the title prominently. We use the full
    relative path (minus a trailing ``.md``) rather than just the file
    stem so the title lines up with the slug column and stays unique:
    a bare stem collapses every ``README.md`` across the tree to the same
    ``README`` title, and loses the directory context for nested files.
    """
    title = rel_path.replace("\\", "/")
    if title.lower().endswith(".md"):
        title = title[:-3]
    return title or rel_path


def push_workspace(
    client: MemoryClientProtocol,
    candidates: Iterable[Candidate],
    *,
    user_id: str,
    skip_unchanged: bool = False,
) -> list[PushOutcome]:
    """Upload each pushable candidate, returning one outcome per row.

    The walker has already pre-classified statically-skippable candidates
    (oversize, slug-unsafe). For everything else this function:

    1. Reads the file (UTF-8 with a replacement fallback so a stray
       binary file produces an ``error`` outcome rather than crashing
       the loop).
    2. If ``skip_unchanged`` is set, fetches the latest server version
       and short-circuits if the SHA-256 matches.
    3. Tries a "append a version" POST first; on a slug-unknown 400 it
       retries with ``create_new_slug=True`` and reports ``created``.
    """
    outcomes: list[PushOutcome] = []
    for cand in candidates:
        if cand.skip_reason:
            status: PushStatus = "skipped-oversize" if "oversize" in cand.skip_reason else "skipped-unsafe"
            outcomes.append(
                PushOutcome(
                    rel_path=cand.rel_path,
                    slug=cand.slug,
                    status=status,
                    detail=cand.skip_reason,
                    size_bytes=cand.size_bytes,
                )
            )
            continue
        outcome = _push_one(client, cand, user_id=user_id, skip_unchanged=skip_unchanged)
        outcomes.append(outcome)
    return outcomes


def _push_one(
    client: MemoryClientProtocol,
    cand: Candidate,
    *,
    user_id: str,
    skip_unchanged: bool,
) -> PushOutcome:
    """Push exactly one file. Returns the outcome instead of raising."""
    try:
        body = cand.path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return PushOutcome(
            rel_path=cand.rel_path,
            slug=cand.slug,
            status="error",
            detail=f"UTF-8 decode failed: {exc}",
            size_bytes=cand.size_bytes,
        )
    except OSError as exc:
        return PushOutcome(
            rel_path=cand.rel_path,
            slug=cand.slug,
            status="error",
            detail=f"read failed: {exc}",
            size_bytes=cand.size_bytes,
        )

    if skip_unchanged:
        try:
            current = client.get_memory_slug(cand.slug, user_id=user_id)
        except AHSAPIError as exc:
            return PushOutcome(
                rel_path=cand.rel_path,
                slug=cand.slug,
                status="error",
                detail=f"lookup failed: {exc}",
                size_bytes=cand.size_bytes,
            )
        if current is not None:
            try:
                remote_body = client.fetch_inline_content(current.artifact_id)
            except AHSAPIError as exc:
                return PushOutcome(
                    rel_path=cand.rel_path,
                    slug=cand.slug,
                    status="error",
                    detail=f"content fetch failed: {exc}",
                    size_bytes=cand.size_bytes,
                )
            if _sha256(remote_body) == _sha256(body):
                return PushOutcome(
                    rel_path=cand.rel_path,
                    slug=cand.slug,
                    status="skipped-unchanged",
                    detail=f"matches v{current.version}",
                    version=current.version,
                    size_bytes=cand.size_bytes,
                )

    title = _title_from(cand.rel_path)
    description = f"Imported from {cand.rel_path} by ahs-memory"
    try:
        data = client.create_memory_artifact(
            slug=cand.slug,
            inline_content=body,
            user_id=user_id,
            title=title,
            description=description,
            create_new_slug=False,
        )
    except AHSAPIError as exc:
        if exc.status_code == 400 and _SLUG_UNKNOWN_HINT in exc.detail:
            # Append failed because the slug is unknown — retry as a fresh slug.
            try:
                data = client.create_memory_artifact(
                    slug=cand.slug,
                    inline_content=body,
                    user_id=user_id,
                    title=title,
                    description=description,
                    create_new_slug=True,
                )
            except AHSAPIError as exc2:
                return PushOutcome(
                    rel_path=cand.rel_path,
                    slug=cand.slug,
                    status="error",
                    detail=str(exc2),
                    size_bytes=cand.size_bytes,
                )
            return PushOutcome(
                rel_path=cand.rel_path,
                slug=cand.slug,
                status="created",
                detail="new slug",
                version=int(data.get("version") or 1),
                size_bytes=cand.size_bytes,
            )
        return PushOutcome(
            rel_path=cand.rel_path,
            slug=cand.slug,
            status="error",
            detail=str(exc),
            size_bytes=cand.size_bytes,
        )

    version = int(data.get("version") or 0)
    status: PushStatus = "created" if version == 1 else "new-version"
    return PushOutcome(
        rel_path=cand.rel_path,
        slug=cand.slug,
        status=status,
        detail=f"v{version}",
        version=version,
        size_bytes=cand.size_bytes,
    )


def diff_workspace(
    client: MemoryClientProtocol,
    candidates: list[Candidate],
    *,
    user_id: str,
) -> list[DiffRow]:
    """3-way diff: candidates ↔ server-side MEMORY rows.

    Skip-reasoned candidates contribute the empty string as their slug,
    which won't match anything on the server — they show up as
    ``local-only`` so the operator still sees them in the diff table.

    Server rows whose slug doesn't appear locally surface as
    ``remote-only`` — useful for spotting drift (memories the agent
    wrote that no markdown file mirrors).

    For overlapping slugs we fetch the inline_content of the latest
    server version and hash-compare against the local file body; an
    exact match is ``unchanged``, anything else is ``changed``.
    """
    remote = client.list_user_memories(user_id)
    remote_by_slug: dict[str, str] = {m.slug: m.artifact_id for m in remote}

    rows: list[DiffRow] = []
    local_slugs: set[str] = set()

    for cand in candidates:
        if not cand.slug:
            rows.append(
                DiffRow(slug="(unsafe)", status="local-only", rel_path=cand.rel_path, size_bytes=cand.size_bytes)
            )
            continue
        local_slugs.add(cand.slug)
        artifact_id = remote_by_slug.get(cand.slug)
        if artifact_id is None:
            rows.append(
                DiffRow(slug=cand.slug, status="local-only", rel_path=cand.rel_path, size_bytes=cand.size_bytes)
            )
            continue
        try:
            local_body = cand.path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Treat unreadable as "changed" so the operator notices.
            rows.append(
                DiffRow(
                    slug=cand.slug,
                    status="changed",
                    rel_path=cand.rel_path,
                    artifact_id=artifact_id,
                    size_bytes=cand.size_bytes,
                )
            )
            continue
        try:
            remote_body = client.fetch_inline_content(artifact_id)
        except AHSAPIError:
            rows.append(
                DiffRow(
                    slug=cand.slug,
                    status="changed",
                    rel_path=cand.rel_path,
                    artifact_id=artifact_id,
                    size_bytes=cand.size_bytes,
                )
            )
            continue
        status: DiffStatus = "unchanged" if _sha256(local_body) == _sha256(remote_body) else "changed"
        rows.append(
            DiffRow(
                slug=cand.slug,
                status=status,
                rel_path=cand.rel_path,
                artifact_id=artifact_id,
                size_bytes=cand.size_bytes,
            )
        )

    for m in remote:
        if m.slug in local_slugs:
            continue
        rows.append(DiffRow(slug=m.slug, status="remote-only", artifact_id=m.artifact_id))

    rows.sort(key=lambda r: (_status_order(r.status), r.slug))
    return rows


_STATUS_ORDER = {
    "local-only": 0,
    "changed": 1,
    "remote-only": 2,
    "unchanged": 3,
}


def _status_order(status: DiffStatus) -> int:
    return _STATUS_ORDER.get(status, 9)
