"""AL arti implementations of the built-in artifact operations.

The ``@shared_tool`` functions in :mod:`agent_artifacts` are thin adapters over
these. Everything here writes to / reads from **AL arti** (``arti.voltcouch.com``)
via :mod:`ypl.external_mcp.arti_client`; the legacy AHS ``agent_artifacts`` table
is no longer written (pure-arti). Session / task / agent linkage is preserved by
**stamping provenance labels** (``session:<id>`` / ``task:<id>`` / ``agent:<name>``)
so "which artifacts did this session/task produce" stays answerable via arti's
label search.

Attribution: writes go out as the caller's per-user arti grant when they have one
(``Authorization: Bearer``), else the arti row's M2M service secret — handled in
``arti_client``.
"""

from __future__ import annotations

import uuid
from typing import Any

from ypl.external_mcp import arti_client
from ypl.external_mcp.arti_client import ArtiToolError, ArtiUnavailable

# arti's artifact_type vocabulary is TEXT / PACKAGE / APP. The legacy pointer
# kinds (CODE_REVIEW / OTHER) are recorded as TEXT carrying the link, tagged with
# a provenance label so they're still findable as what they were.
_POINTER_KINDS = {"CODE_REVIEW", "OTHER"}


def provenance_labels(
    *, session_id: uuid.UUID | None, agent_name: str | None, task_id: uuid.UUID | None
) -> list[str]:
    """The session/agent/task tags stamped on every write for linkage."""
    out: list[str] = []
    if session_id:
        out.append(f"session:{session_id}")
    if agent_name:
        out.append(f"agent:{agent_name}")
    if task_id:
        out.append(f"task:{task_id}")
    return out


def _merge_labels(user_labels: list[str] | None, prov: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for label in (user_labels or []) + prov:
        if label:
            seen.setdefault(label, None)
    return list(seen)


async def add_text(
    *,
    user_id: str | None,
    session_id: uuid.UUID | None,
    title: str,
    content: str,
    content_type: str,
    named_slug: str | None,
    create_new_slug: bool,
    description: str | None,
    labels: list[str] | None,
    prov: list[str],
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "title": title,
        "content": content,
        "content_type": content_type,
        "artifact_type": "TEXT",
        "labels": _merge_labels(labels, prov),
    }
    if named_slug:
        args["named_slug"] = named_slug
        if create_new_slug:
            args["ensure_new"] = True
    if description:
        args["description"] = description
    res = await arti_client.call_tool("add_artifact", args, user_id=user_id, agent_session_id=session_id)
    return {
        "success": True,
        "artifact_id": res.get("artifact_id"),
        "url": res.get("url"),
        "title": res.get("title", title),
        "slug": res.get("named_slug"),
        "version": res.get("version"),
        "content_type": res.get("content_type", content_type),
        "labels": res.get("labels", []),
        "message": f"Artifact '{title}' (TEXT) saved to AL arti at {res.get('url')}.",
    }


async def add_pointer(
    *,
    user_id: str | None,
    session_id: uuid.UUID | None,
    artifact_type: str,
    title: str,
    url: str,
    description: str | None,
    labels: list[str] | None,
    prov: list[str],
) -> dict[str, Any]:
    body = f"[{title}]({url})"
    if description:
        body += f"\n\n{description}"
    marker = artifact_type.lower()  # "code_review" / "other"
    res = await arti_client.call_tool(
        "add_artifact",
        {
            "title": title,
            "content": body,
            "content_type": "text/markdown",
            "artifact_type": "TEXT",
            "description": description or f"{artifact_type} pointer",
            "labels": _merge_labels((labels or []) + ["pointer", marker], prov),
        },
        user_id=user_id,
        agent_session_id=session_id,
    )
    return {
        "success": True,
        "artifact_id": res.get("artifact_id"),
        "url": url,  # agents announce the external pointer (PR/doc link), not the arti record
        "arti_url": res.get("url"),
        "title": res.get("title", title),
        "labels": res.get("labels", []),
        "message": f"{artifact_type} pointer '{title}' recorded in AL arti ({res.get('url')}); target: {url}",
    }


async def add_version(
    *,
    user_id: str | None,
    session_id: uuid.UUID | None,
    slug: str,
    content: str,
    content_type: str,
    title: str,
    description: str | None,
    labels: list[str] | None,
    prov: list[str],
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "title": title,
        "content": content,
        "content_type": content_type,
        "artifact_type": "TEXT",
        "named_slug": slug,
        "labels": _merge_labels(labels, prov),
    }
    if description:
        args["description"] = description
    res = await arti_client.call_tool("add_artifact", args, user_id=user_id, agent_session_id=session_id)
    return {
        "success": True,
        "artifact_id": res.get("artifact_id"),
        "url": res.get("url"),
        "slug": res.get("named_slug", slug),
        "version": res.get("version"),
        "content_type": res.get("content_type", content_type),
        "labels": res.get("labels", []),
        "message": f"Artifact '{slug}' saved as version {res.get('version')} in AL arti at {res.get('url')}.",
    }


async def update_meta(
    *,
    user_id: str | None,
    ident: str,
    title: str | None,
    labels: list[str] | None,
) -> dict[str, Any]:
    args: dict[str, Any] = {"ident": ident}
    if title is not None:
        args["title"] = title
    if labels is not None:
        args["labels"] = labels
    res = await arti_client.call_tool("update_artifact", args, user_id=user_id)
    return {
        "success": True,
        "artifact_id": res.get("artifact_id", ident),
        "message": f"Artifact '{res.get('title', ident)}' metadata updated in AL arti.",
    }


def _row(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": a.get("artifact_id"),
        "artifact_type": a.get("artifact_type"),
        "title": a.get("title"),
        "url": a.get("url"),
        "description": a.get("description"),
        "slug": a.get("named_slug"),
        "version": a.get("version"),
        "created_at": a.get("created_at"),
        "labels": a.get("labels", []),
    }


async def list_for_session(
    *, user_id: str | None, session_id: uuid.UUID | None, artifact_type: str | None, limit: int
) -> dict[str, Any]:
    if session_id is None:
        return {"success": True, "artifacts": [], "count": 0}
    args: dict[str, Any] = {"labels": [f"session:{session_id}"], "limit": limit, "order_by": "created_at", "order_dir": "desc"}
    if artifact_type == "TEXT":
        args["type"] = "TEXT"
    res = await arti_client.call_tool("list_artifacts", args, user_id=user_id, agent_session_id=session_id)
    rows = [_row(a) for a in (res.get("artifacts") or [])]
    return {"success": True, "artifacts": rows, "count": len(rows)}


async def list_versions(*, user_id: str | None, slug: str) -> dict[str, Any]:
    res = await arti_client.call_tool("list_artifact_versions", {"slug": slug}, user_id=user_id)
    rows = [
        {
            "version": a.get("version"),
            "artifact_id": a.get("artifact_id"),
            "title": a.get("title"),
            "url": a.get("url"),
            "content_type": a.get("content_type"),
            "created_at": a.get("created_at"),
            "labels": a.get("labels", []),
        }
        for a in (res.get("versions") or [])
    ]
    return {"success": True, "slug": slug, "versions": rows, "count": len(rows)}


async def search(
    *, user_id: str | None, query: str, artifact_type: str | None, limit: int, offset: int, include_archived: bool
) -> dict[str, Any]:
    args: dict[str, Any] = {"q": query, "limit": limit, "offset": offset, "include_archived": include_archived}
    if artifact_type == "TEXT":
        args["type"] = "TEXT"
    res = await arti_client.call_tool("search_artifacts", args, user_id=user_id)
    rows = [_row(a) for a in (res.get("artifacts") or [])]
    return {"success": True, "results": rows, "count": len(rows)}


async def read(*, user_id: str | None, ident: str, version: int | None) -> dict[str, Any] | None:
    """Return the read dict, or ``None`` if arti reports it missing (caller
    then falls back to the legacy store for un-migrated artifacts)."""
    meta_args: dict[str, Any] = {"ident": ident}
    if version is not None:
        meta_args["version"] = version
    try:
        meta = await arti_client.call_tool("get_artifact", meta_args, user_id=user_id)
    except ArtiToolError as exc:
        if arti_client.not_found(exc):
            return None
        raise
    read_args: dict[str, Any] = {"ident": ident}
    if version is not None:
        read_args["version"] = version
    raw = await arti_client.call_tool_raw("read_artifact", read_args, user_id=user_id)
    content_blocks = raw.get("content") or []
    body = content_blocks[0].get("text", "") if content_blocks else ""
    return {
        "artifact_id": meta.get("artifact_id"),
        "url": meta.get("url"),
        "title": meta.get("title"),
        "description": meta.get("description"),
        "content": body,
        "content_type": raw.get("content_type") or meta.get("content_type"),
        "slug": meta.get("named_slug"),
        "version": meta.get("version"),
        "labels": meta.get("labels", []),
    }


async def url_of(*, user_id: str | None, ident: str) -> dict[str, Any] | None:
    try:
        meta = await arti_client.call_tool("get_artifact", {"ident": ident}, user_id=user_id)
    except ArtiToolError as exc:
        if arti_client.not_found(exc):
            return None
        raise
    return {
        "success": True,
        "artifact_id": meta.get("artifact_id"),
        "url": meta.get("url"),
        "title": meta.get("title"),
        "artifact_type": meta.get("artifact_type"),
    }


async def archive(*, user_id: str | None, ident: str) -> bool | None:
    """True if archived; None if arti reports it missing (caller may fall back)."""
    try:
        await arti_client.call_tool("archive_artifact", {"ident": ident}, user_id=user_id)
        return True
    except ArtiToolError as exc:
        if arti_client.not_found(exc):
            return None
        raise


__all__ = [
    "ArtiUnavailable",
    "ArtiToolError",
    "provenance_labels",
    "add_text",
    "add_pointer",
    "add_version",
    "update_meta",
    "list_for_session",
    "list_versions",
    "search",
    "read",
    "url_of",
    "archive",
]
