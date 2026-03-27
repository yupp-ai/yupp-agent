"""Shared GCS sync utilities for manifest-based file uploads.

Provides a reusable rsync-like sync mechanism used by both session persistence
(attachments + history) and agent memory persistence. Only new or modified files
are uploaded, tracked via a local JSON manifest.

Concurrency safety:
- In-process: asyncio.Lock keyed by resource prevents concurrent syncs.
- Cross-process (same node): flock on a lockfile in base_dir serializes syncs
  across different server processes or pods sharing the same filesystem.
- Cross-node: No locking. The manifest-based approach is additive — worst case
  two nodes upload the same file, which is idempotent in GCS.
"""

import asyncio
import fcntl
import json
import logging
import mimetypes
import os

import aiohttp
from gcloud.aio.storage import Storage
from tenacity import after_log, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ypl.structured_logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------


def load_manifest(base_dir: str, manifest_filename: str) -> dict[str, float]:
    """Load the sync manifest mapping ``relative_path -> mtime``."""
    path = os.path.join(base_dir, manifest_filename)
    try:
        with open(path) as f:
            data: dict[str, float] = json.load(f)
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_manifest(base_dir: str, manifest_filename: str, manifest: dict[str, float]) -> None:
    """Persist the sync manifest atomically."""
    path = os.path.join(base_dir, manifest_filename)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(manifest, f)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------


def collect_changed_files(
    base_dir: str,
    subdirs: tuple[str, ...],
    manifest: dict[str, float],
    manifest_filename: str = "",
    skip_symlinks: bool = True,
    skip_hardlinks: bool = False,
) -> list[tuple[str, str, float]]:
    """Return files that are new or modified since last sync.

    Args:
        base_dir: Root directory containing the subdirs.
        subdirs: Subdirectories within base_dir to scan. Empty tuple means scan base_dir directly.
        manifest: Previous manifest mapping rel_path -> mtime.
        manifest_filename: Manifest filename to skip during collection.
        skip_symlinks: Skip symbolic links (security).
        skip_hardlinks: Skip hard links (security).

    Returns:
        List of ``(absolute_path, relative_path, mtime)`` tuples.
    """
    to_sync: list[tuple[str, str, float]] = []

    # Determine which directories to walk
    if subdirs:
        walk_dirs = [os.path.join(base_dir, s) for s in subdirs]
    else:
        walk_dirs = [base_dir]

    for dir_path in walk_dirs:
        if not os.path.isdir(dir_path):
            continue
        for root, _, files in os.walk(dir_path, followlinks=False):
            for fname in files:
                if manifest_filename and fname == manifest_filename:
                    continue
                if fname.endswith(".tmp"):
                    continue
                abs_path = os.path.join(root, fname)
                if skip_symlinks and os.path.islink(abs_path):
                    continue
                if skip_hardlinks:
                    try:
                        if os.stat(abs_path, follow_symlinks=False).st_nlink > 1:
                            logger.warning("Skipping hard-linked file", path=abs_path)
                            continue
                    except OSError:
                        continue
                rel_path = os.path.relpath(abs_path, base_dir)
                try:
                    mtime = os.path.getmtime(abs_path)
                except OSError:
                    continue
                if manifest.get(rel_path) != mtime:
                    to_sync.append((abs_path, rel_path, mtime))
    return to_sync


def guess_content_type(filename: str) -> str:
    ct, _ = mimetypes.guess_type(filename)
    return ct or "application/octet-stream"


# ---------------------------------------------------------------------------
# GCS upload with retry
# ---------------------------------------------------------------------------

_retry_gcs_upload = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=1, max=10),
    retry=retry_if_exception_type((TimeoutError, aiohttp.ClientError, OSError)),
    after=after_log(logging.getLogger(__name__), logging.WARNING),
    reraise=True,
)


@_retry_gcs_upload
async def upload_file(
    storage: Storage,
    bucket: str,
    object_name: str,
    data: bytes,
    content_type: str,
) -> None:
    """Upload a single file to GCS with retry on transient errors."""
    await storage.upload(
        bucket=bucket,
        object_name=object_name,
        file_data=data,
        content_type=content_type,
    )


# ---------------------------------------------------------------------------
# Generic sync function
# ---------------------------------------------------------------------------

# Per-key locks to prevent concurrent syncs for the same resource.
_sync_locks: dict[str, asyncio.Lock] = {}


def _get_sync_lock(key: str) -> asyncio.Lock:
    if key not in _sync_locks:
        _sync_locks[key] = asyncio.Lock()
    return _sync_locks[key]


async def sync_dir_to_gcs(
    *,
    base_dir: str,
    subdirs: tuple[str, ...],
    bucket: str,
    gcs_prefix: str,
    lock_key: str,
    manifest_filename: str = ".gcs_sync_manifest.json",
    skip_symlinks: bool = True,
    skip_hardlinks: bool = False,
    log_label: str = "files",
) -> int:
    """Sync local directories to GCS using manifest-based change detection.

    Uses two layers of locking:
    1. In-process asyncio.Lock (keyed by lock_key) for coroutine safety.
    2. Cross-process flock on a lockfile in base_dir for multi-process safety.

    Args:
        base_dir: Local root directory.
        subdirs: Subdirectories within base_dir to sync. Empty tuple = sync base_dir itself.
        bucket: GCS bucket name.
        gcs_prefix: GCS object name prefix (files are uploaded as ``{gcs_prefix}/{rel_path}``).
        lock_key: Key for per-resource lock to prevent concurrent syncs.
        manifest_filename: Name of the manifest file.
        skip_symlinks: Skip symbolic links.
        skip_hardlinks: Skip hard links.
        log_label: Label for log messages (e.g., "session files", "agent memory").

    Returns:
        Number of files uploaded.
    """
    if not os.path.isdir(base_dir):
        return 0

    # Layer 1: in-process asyncio lock (prevents concurrent coroutines).
    lock = _get_sync_lock(lock_key)
    async with lock:
        # Layer 2: cross-process file lock (prevents concurrent processes on the same node).
        lock_path = os.path.join(base_dir, ".gcs_sync.lock")
        lock_fd: int | None = None
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
            # Non-blocking try: if another process holds the lock, skip this sync.
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                logger.debug("Skipping GCS sync, another process holds the lock", lock_key=lock_key)
                return 0

            return await _sync_dir_to_gcs_inner(
                base_dir=base_dir,
                subdirs=subdirs,
                bucket=bucket,
                gcs_prefix=gcs_prefix,
                manifest_filename=manifest_filename,
                skip_symlinks=skip_symlinks,
                skip_hardlinks=skip_hardlinks,
                log_label=log_label,
            )
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)
                except OSError:
                    pass


async def _sync_dir_to_gcs_inner(
    *,
    base_dir: str,
    subdirs: tuple[str, ...],
    bucket: str,
    gcs_prefix: str,
    manifest_filename: str,
    skip_symlinks: bool,
    skip_hardlinks: bool,
    log_label: str,
) -> int:
    """Inner sync logic, called under both asyncio and file locks."""
    manifest = load_manifest(base_dir, manifest_filename)
    to_sync = collect_changed_files(
        base_dir,
        subdirs,
        manifest,
        manifest_filename=manifest_filename,
        skip_symlinks=skip_symlinks,
        skip_hardlinks=skip_hardlinks,
    )
    if not to_sync:
        return 0

    uploaded = 0
    async with aiohttp.ClientSession() as http_session, Storage(session=http_session) as storage:  # type: ignore[arg-type]
        for abs_path, rel_path, mtime in to_sync:
            gcs_object = f"{gcs_prefix}/{rel_path}"
            try:
                with open(abs_path, "rb") as f:
                    data = f.read()
                content_type = guess_content_type(abs_path)
                await upload_file(storage, bucket, gcs_object, data, content_type)
                manifest[rel_path] = mtime
                uploaded += 1
            except Exception:
                logger.error(
                    f"Failed to upload {log_label} to GCS",
                    rel_path=rel_path,
                    gcs_object=gcs_object,
                    exc_info=True,
                )

    if uploaded > 0:
        try:
            save_manifest(base_dir, manifest_filename, manifest)
        except Exception:
            logger.error(f"Failed to save {log_label} sync manifest", exc_info=True)

        logger.info(
            f"Synced {log_label} to GCS",
            bucket=bucket,
            files_uploaded=uploaded,
            files_total=len(to_sync),
        )

    return uploaded
