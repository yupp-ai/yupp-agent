"""DB-authoritative materialization of MEMORY artifacts onto the sandbox disk.

Two halves:

1. **Session-start materialization** — fetch every MEMORY artifact visible
   to ``(caller_user_id, caller_agent_name)`` (topic + own user + own
   agent) and write each to ``{workspace}/agent_memories/{scope}/{slug}.md``.
2. **Write-through update** — after a successful save against the DB,
   refresh the corresponding on-disk file via atomic temp+rename so the
   agent's in-turn ``cat`` / ``grep`` see the new content.

The DB is the single source of truth; the disk copy is a per-session
working cache that the runtime is free to discard. There is no GCS step,
no manifest file, and no symlink dance.

See ``docs/designs/unify-memory-into-artifacts.md`` (artifact slug
``unify-memory-into-artifacts`` v2) section *"Disk as materialized
working copy"* for the rationale.
"""

from __future__ import annotations
import os
import tempfile

from ypl.agent_harness_service.artifact_store import (
    ArtifactError,
    list_artifacts,
    read_artifact_content,
)
from ypl.agent_harness_service.memory_slug import is_safe_slug
from ypl.agent_harness_service.memory_store import MemoryCallerContext
from ypl.db.agent_harness import AgentArtifactType
from ypl.structured_logger import get_logger

logger = get_logger()

# Sandbox-relative root for materialized memory.
MEMORY_ROOT = "agent_memories"

# Subdirectory per scope. Mirrors the three valid ``memory_scope`` values
# in :data:`ypl.agent_harness_service.memory_store.VALID_MEMORY_SCOPES`.
SCOPE_SUBDIRS: tuple[str, str, str] = ("topic", "user", "agent")


# ---------------------------------------------------------------------------
# Path / safety helpers
# ---------------------------------------------------------------------------


def _safe_relpath_for(scope: str | None, slug: str | None) -> str | None:
    """Return ``{scope}/{slug}.md`` if both are valid; else ``None``.

    Rejects scopes outside the canonical three, empty / non-conforming
    slugs, and any slug that would escape the memory root via ``..``.
    Slug validation is delegated to
    :func:`ypl.agent_harness_service.memory_slug.is_safe_slug` so the
    server-side materializer and the ``ahs-memory`` CLI share one rule
    set.
    """
    if scope not in SCOPE_SUBDIRS or not slug or not is_safe_slug(slug):
        return None
    return os.path.join(scope, f"{slug}.md")


def _atomic_write(path: str, content: bytes) -> None:
    """Write ``content`` to ``path`` via temp file + ``os.replace``.

    Creates the parent directory if needed. The temp file lives in the
    same directory as the target so ``os.replace`` is a true atomic
    rename (same filesystem). On any error the temp file is unlinked.
    """
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp.", dir=parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def memory_file_path(workspace: str, scope: str, slug: str) -> str | None:
    """Return the absolute on-disk path for ``(scope, slug)`` or ``None``.

    ``None`` means the inputs failed validation — the caller should NOT
    fall back to a manually-constructed path.
    """
    rel = _safe_relpath_for(scope, slug)
    if rel is None:
        return None
    return os.path.join(workspace, MEMORY_ROOT, rel)


def write_memory_file(workspace: str, scope: str, slug: str, content: str) -> str | None:
    """Atomically write a memory file. Returns the path written, or ``None``.

    ``None`` is returned when the scope/slug is invalid (logged as a
    warning). All other I/O errors propagate to the caller — the
    write-through path treats those as best-effort and logs without
    failing the DB save.
    """
    target = memory_file_path(workspace, scope, slug)
    if target is None:
        logger.warning(
            "Refusing to write memory file with invalid scope/slug",
            scope=scope,
            slug=slug,
        )
        return None
    _atomic_write(target, content.encode("utf-8"))
    return target


# ---------------------------------------------------------------------------
# Session-start materialization
# ---------------------------------------------------------------------------


async def materialize_memory_for_session(
    *,
    workspace: str,
    caller: MemoryCallerContext,
    limit: int = 500,
) -> int:
    """Materialize MEMORY artifacts visible to ``caller`` into ``workspace``.

    Layout::

        {workspace}/agent_memories/topic/{slug}.md
        {workspace}/agent_memories/user/{slug}.md      (caller.user_id only)
        {workspace}/agent_memories/agent/{slug}.md     (caller.agent_name only)

    Always creates the three scope subdirectories so the agent can
    ``ls agent_memories/`` and see the structure even when no rows are
    present. Returns the number of files written. Best-effort: per-row
    failures are logged and skipped so a single bad artifact can't block
    session start.
    """
    memory_root = os.path.join(workspace, MEMORY_ROOT)
    os.makedirs(memory_root, exist_ok=True)
    for sub in SCOPE_SUBDIRS:
        os.makedirs(os.path.join(memory_root, sub), exist_ok=True)

    if not caller.has_identity:
        # No identity → only globally readable ``topic`` rows are visible.
        # The list_artifacts read filter handles this; we still issue the
        # query so topics get materialized for non-personal agents.
        pass

    try:
        artifacts = await list_artifacts(
            artifact_type=AgentArtifactType.MEMORY,
            memory_caller=caller,
            limit=limit,
        )
    except Exception:
        logger.exception(
            "Failed to fetch MEMORY artifacts for session materialization",
            user_id=caller.user_id,
            agent_name=caller.agent_name,
        )
        return 0

    written = 0
    for artifact in artifacts:
        rel = _safe_relpath_for(artifact.memory_scope, artifact.named_slug)
        if rel is None:
            logger.debug(
                "Skipping MEMORY artifact with non-materializable scope/slug",
                artifact_id=str(artifact.agent_artifact_id),
                scope=artifact.memory_scope,
                slug=artifact.named_slug,
            )
            continue
        try:
            data, _ctype = await read_artifact_content(artifact)
        except ArtifactError:
            logger.warning(
                "Failed to read MEMORY artifact body during materialization",
                artifact_id=str(artifact.agent_artifact_id),
                exc_info=True,
            )
            continue
        except Exception:
            logger.warning(
                "Unexpected error reading MEMORY artifact body during materialization",
                artifact_id=str(artifact.agent_artifact_id),
                exc_info=True,
            )
            continue
        try:
            _atomic_write(os.path.join(memory_root, rel), data)
            written += 1
        except OSError:
            logger.warning(
                "Failed to write MEMORY artifact to sandbox",
                artifact_id=str(artifact.agent_artifact_id),
                target=rel,
                exc_info=True,
            )

    logger.info(
        "Materialized MEMORY artifacts to sandbox",
        workspace=workspace,
        files_written=written,
        artifacts_considered=len(artifacts),
        user_id=caller.user_id,
        agent_name=caller.agent_name,
    )
    return written
