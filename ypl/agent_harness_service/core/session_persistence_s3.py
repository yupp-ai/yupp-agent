"""S3 persistence for agent session workspace directories.

Mirrors :mod:`ypl.agent_harness_service.core.gcs_sync` but targets Amazon S3
(or any S3-compatible endpoint such as MinIO / LocalStack) using the
``aioboto3`` async client. Reuses the manifest / change-detection / locking
helpers from ``gcs_sync.py`` so the two backends stay byte-for-byte
compatible at the on-disk level.

Concurrency safety follows the same model as ``gcs_sync``:
- In-process: asyncio.Lock keyed by resource prevents concurrent syncs.
- Cross-process (same node): flock on a lockfile in base_dir serializes syncs
  across different server processes or pods sharing the same filesystem.
- Cross-node: No locking. The manifest-based approach is additive — worst
  case two nodes upload the same key, which is idempotent in S3.
"""

from __future__ import annotations
import asyncio
import fcntl
import logging
import os
from typing import Any

import aioboto3
from botocore.exceptions import BotoCoreError, ClientError
from tenacity import after_log, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

# Reuse the backend-agnostic helpers from gcs_sync — identical manifest +
# file-walk semantics across both backends.
from ypl.agent_harness_service.core.gcs_sync import (
    collect_changed_files,
    guess_content_type,
    load_manifest,
    save_manifest,
)
from ypl.structured_logger import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# S3 upload with retry
# ---------------------------------------------------------------------------

_retry_s3_upload = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=1, max=10),
    retry=retry_if_exception_type((TimeoutError, BotoCoreError, ClientError, OSError)),
    after=after_log(logging.getLogger(__name__), logging.WARNING),
    reraise=True,
)


@_retry_s3_upload
async def upload_file(
    client: Any,
    bucket: str,
    object_name: str,
    data: bytes,
    content_type: str,
) -> None:
    """Upload a single file to S3 with retry on transient errors."""
    await client.put_object(
        Bucket=bucket,
        Key=object_name,
        Body=data,
        ContentType=content_type,
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


async def sync_dir_to_s3(
    *,
    base_dir: str,
    subdirs: tuple[str, ...],
    bucket: str,
    s3_prefix: str,
    lock_key: str,
    region: str | None = None,
    endpoint_url: str | None = None,
    manifest_filename: str = ".s3_sync_manifest.json",
    skip_symlinks: bool = True,
    skip_hardlinks: bool = False,
    log_label: str = "files",
) -> int:
    """Sync local directories to S3 using manifest-based change detection.

    Uses two layers of locking:
    1. In-process asyncio.Lock (keyed by lock_key) for coroutine safety.
    2. Cross-process flock on a lockfile in base_dir for multi-process safety.

    Args:
        base_dir: Local root directory.
        subdirs: Subdirectories within base_dir to sync. Empty tuple = sync
            base_dir itself.
        bucket: S3 bucket name.
        s3_prefix: S3 key prefix (files are uploaded as ``{s3_prefix}/{rel_path}``).
        lock_key: Key for per-resource lock to prevent concurrent syncs.
        region: AWS region (e.g. ``us-west-2``).
        endpoint_url: Optional S3 endpoint override (MinIO / LocalStack).
        manifest_filename: Name of the manifest file. Defaults to a
            backend-specific filename so the GCS and S3 manifests do not
            collide if a deployment ever flips between backends.
        skip_symlinks: Skip symbolic links.
        skip_hardlinks: Skip hard links.
        log_label: Label for log messages (e.g., "session files").

    Returns:
        Number of files uploaded.
    """
    if not os.path.isdir(base_dir):
        return 0

    if not bucket:
        logger.debug("Skipping S3 sync (empty bucket)", log_label=log_label)
        return 0

    # Layer 1: in-process asyncio lock (prevents concurrent coroutines).
    lock = _get_sync_lock(lock_key)
    async with lock:
        # Layer 2: cross-process file lock (prevents concurrent processes on the same node).
        lock_path = os.path.join(base_dir, ".s3_sync.lock")
        lock_fd: int | None = None
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                logger.debug("Skipping S3 sync, another process holds the lock", lock_key=lock_key)
                return 0

            return await _sync_dir_to_s3_inner(
                base_dir=base_dir,
                subdirs=subdirs,
                bucket=bucket,
                s3_prefix=s3_prefix,
                region=region,
                endpoint_url=endpoint_url,
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


async def _sync_dir_to_s3_inner(
    *,
    base_dir: str,
    subdirs: tuple[str, ...],
    bucket: str,
    s3_prefix: str,
    region: str | None,
    endpoint_url: str | None,
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

    client_kwargs: dict[str, Any] = {}
    if region is not None:
        client_kwargs["region_name"] = region
    if endpoint_url is not None:
        client_kwargs["endpoint_url"] = endpoint_url

    uploaded = 0
    session = aioboto3.Session()
    async with session.client("s3", **client_kwargs) as client:
        for abs_path, rel_path, mtime in to_sync:
            s3_object = f"{s3_prefix}/{rel_path}"
            try:
                with open(abs_path, "rb") as f:
                    data = f.read()
                content_type = guess_content_type(abs_path)
                await upload_file(client, bucket, s3_object, data, content_type)
                manifest[rel_path] = mtime
                uploaded += 1
            except Exception:
                logger.error(
                    f"Failed to upload {log_label} to S3",
                    rel_path=rel_path,
                    s3_object=s3_object,
                    exc_info=True,
                )

    if uploaded > 0:
        try:
            save_manifest(base_dir, manifest_filename, manifest)
        except Exception:
            logger.error(f"Failed to save {log_label} sync manifest", exc_info=True)

        logger.info(
            f"Synced {log_label} to S3",
            bucket=bucket,
            files_uploaded=uploaded,
            files_total=len(to_sync),
        )

    return uploaded
