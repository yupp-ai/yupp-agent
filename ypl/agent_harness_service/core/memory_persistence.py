"""GCS persistence for per-agent memory directories.

After each agent turn, syncs the agent's ``agent_memories/`` directory to
``gs://yupp-agents/agent_memory/{agent_name}/`` using the shared manifest-based
sync utility.

After GCS sync, any new or modified ``.md`` files are indexed into the
``agent_memory_sections`` / ``agent_memory_section_embeddings`` tables with
topic prefix ``PRIVATE/{agent_name}/...`` so they become searchable via the
agent memory search pipeline.

Unlike session persistence, memory sync is NOT disabled in local dev
so that developers can test the full flow.
"""

import json
import os

from ypl.agent_harness_service.common.constants import (
    AHS_GCS_MEMORY_BUCKET,
    AHS_GCS_MEMORY_PREFIX,
    AHS_MEMORIES_DIR,
)
from ypl.agent_harness_service.core.gcs_sync import sync_dir_to_gcs
from ypl.backend.llm.agent_memory_search import PRIVATE_TOPIC_PREFIX
from ypl.structured_logger import get_logger

logger = get_logger()
_INDEX_MANIFEST_FILENAME = ".private_memory_index_manifest.json"


async def sync_agent_memory_to_gcs(agent_name: str) -> int:
    """Sync an agent's memory directory to GCS.

    Uploads only files that are new or modified since the last sync
    (determined via a local manifest file). Uses a per-agent lock to
    prevent concurrent uploads for the same agent.

    After GCS sync, indexes any new or modified markdown files into the
    search index with topic ``PRIVATE/{agent_name}/{filename_stem}``.

    Target layout::

        gs://{AHS_GCS_MEMORY_BUCKET}/{AHS_GCS_MEMORY_PREFIX}/{agent_name}/...

    Args:
        agent_name: The agent name (e.g., "yuppclaw-alice").

    Returns:
        Number of files uploaded to GCS.
    """
    memory_dir = os.path.join(AHS_MEMORIES_DIR, agent_name, "agent_memories")
    uploaded = await sync_dir_to_gcs(
        base_dir=memory_dir,
        subdirs=(),  # Sync the memory_dir itself (no subdirs)
        bucket=AHS_GCS_MEMORY_BUCKET,
        gcs_prefix=f"{AHS_GCS_MEMORY_PREFIX}/{agent_name}",
        lock_key=f"memory:{agent_name}",
        manifest_filename=".gcs_memory_sync_manifest.json",
        skip_symlinks=True,
        skip_hardlinks=True,
        log_label=f"agent {agent_name} memory",
    )

    # Index changed markdown files into the search index (best-effort).
    try:
        await _index_changed_memory_files(agent_name, memory_dir)
    except Exception:
        logger.warning(
            "Private memory indexing failed",
            agent_name=agent_name,
            exc_info=True,
        )

    return uploaded


async def _index_changed_memory_files(agent_name: str, memory_dir: str) -> None:
    """Index new/modified markdown files from the agent's private memory dir.

    Maintains a separate index manifest (mtime-based) so only changed files
    are re-embedded. Indexing is idempotent via upserts in the DB.
    """
    if not os.path.isdir(memory_dir):
        return

    # Load index manifest
    manifest_path = os.path.join(memory_dir, _INDEX_MANIFEST_FILENAME)
    manifest: dict[str, float] = {}
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Collect current .md files on disk and detect changed ones
    current_files_on_disk: set[str] = set()
    changed_files: list[tuple[str, str, float]] = []  # (abs_path, rel_path, mtime)
    for root, _, files in os.walk(memory_dir, followlinks=False):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            abs_path = os.path.join(root, fname)
            if os.path.islink(abs_path):
                continue
            rel_path = os.path.relpath(abs_path, memory_dir)
            current_files_on_disk.add(rel_path)
            try:
                mtime = os.path.getmtime(abs_path)
            except OSError:
                continue
            if manifest.get(rel_path) != mtime:
                changed_files.append((abs_path, rel_path, mtime))

    # Detect deleted files: manifest keys no longer on disk
    deleted_files = set(manifest.keys()) - current_files_on_disk

    if not changed_files and not deleted_files:
        return

    from ypl.backend.llm.agent_memory_indexing import index_topic_sections

    manifest_changed = False

    # Soft-delete indexed sections for files removed from disk
    for rel_path in deleted_files:
        topic_stem = rel_path.removesuffix(".md")
        topic = f"{PRIVATE_TOPIC_PREFIX}/{agent_name}/{topic_stem}"
        try:
            await index_topic_sections(topic=topic, content="", agent_name=agent_name)
            del manifest[rel_path]
            manifest_changed = True
        except Exception:
            logger.warning(
                "Failed to soft-delete indexed sections for deleted file",
                topic=topic,
                agent_name=agent_name,
                exc_info=True,
            )

    # Index new/modified files
    indexed = 0
    for abs_path, rel_path, mtime in changed_files:
        # Build topic: PRIVATE/{agent_name}/{filename_without_ext}
        # e.g., PRIVATE/yuppclaw-alice/user_preferences
        topic_stem = rel_path.removesuffix(".md")
        topic = f"{PRIVATE_TOPIC_PREFIX}/{agent_name}/{topic_stem}"

        try:
            with open(abs_path) as f:
                content = f.read()
        except OSError:
            logger.warning("Failed to read private memory file", path=abs_path)
            continue

        try:
            await index_topic_sections(
                topic=topic,
                content=content,
                agent_name=agent_name,
            )
            manifest[rel_path] = mtime
            indexed += 1
            manifest_changed = True
        except Exception:
            logger.warning(
                "Failed to index private memory file",
                topic=topic,
                agent_name=agent_name,
                exc_info=True,
            )

    # Save index manifest
    if manifest_changed:
        try:
            tmp_path = manifest_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(manifest, f)
            os.replace(tmp_path, manifest_path)
        except OSError:
            logger.warning("Failed to save private memory index manifest", exc_info=True)

        logger.info(
            "Indexed private memory files",
            agent_name=agent_name,
            files_indexed=indexed,
            files_deleted=len(deleted_files),
            files_total=len(changed_files),
        )
