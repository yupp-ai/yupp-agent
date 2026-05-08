"""BlobStore — pluggable blob-storage abstraction.

Used by the artifact system to store textual artifact content and
attachments. Logical path convention is set by the caller — the store
just reads/writes at that path.

The artifact service (``ypl/agent_harness_service/artifact_store.py``)
uses this convention::

    {uuid[:2]}/{uuid}/{uuid}.{ext}    # main artifact content
    {uuid[:2]}/{uuid}/{filename}      # attachments

Backend selection is driven by ``settings.BLOB_STORE_ENGINE``:

- ``"local"`` → :class:`~ypl.backend.utils.blob_store_local.LocalBlobStore`
  Writes under ``BLOB_STORE_LOCAL_DIR``.
- ``"gcs"``   → :class:`~ypl.backend.utils.blob_store_gcs.GCSBlobStore`
  Writes under ``gs://{GCS_BUCKET_NAME}/``.
- ``"s3"``    → :class:`~ypl.backend.utils.blob_store_s3.S3BlobStore`
  Writes under ``s3://{S3_BUCKET_NAME}/``.
"""

from __future__ import annotations
from typing import Protocol, runtime_checkable

from ypl.backend.config import settings


@runtime_checkable
class BlobStore(Protocol):
    """Protocol for pluggable blob storage backends."""

    async def upload(
        self,
        path: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Store *data* at the logical *path*.

        Creates any intermediate path segments as needed.  Overwrites an
        existing object silently.
        """
        ...

    async def download(self, path: str) -> bytes:
        """Return the raw bytes stored at *path*.

        Raises:
            FileNotFoundError: if *path* does not exist.
        """
        ...

    async def get_size(self, path: str) -> int:
        """Return the byte-length of the object at *path*.

        Raises:
            FileNotFoundError: if *path* does not exist.
        """
        ...

    async def get_access_url(self, path: str, expiry_seconds: int = 3 * 24 * 3600) -> str:
        """Return a URL suitable for serving the object at *path*.

        For GCS this is a signed URL; for local it is a plain HTTP URL
        constructed from the configured base URL.

        Args:
            path: Logical path of the object.
            expiry_seconds: Hint for the desired URL lifetime in seconds.
                Backends make a best-effort attempt to honour this value but
                are not required to do so (e.g. ``GCSBlobStore`` uses an
                internally configured TTL).

        Raises:
            ValueError: if the backend cannot generate a URL (e.g. no base URL
                configured for LocalBlobStore).
        """
        ...

    async def exists(self, path: str) -> bool:
        """Return True if an object exists at *path*, False otherwise."""
        ...

    async def delete(self, path: str) -> None:
        """Remove the object at *path*.

        Raises:
            FileNotFoundError: if *path* does not exist.
        """
        ...


def get_blob_store() -> BlobStore:
    """Factory that returns the configured BlobStore backend.

    Reads ``settings.BLOB_STORE_ENGINE`` to choose the implementation:
    - ``"local"`` → :class:`~ypl.backend.utils.blob_store_local.LocalBlobStore`
    - ``"gcs"``   → :class:`~ypl.backend.utils.blob_store_gcs.GCSBlobStore`
    - ``"s3"``    → :class:`~ypl.backend.utils.blob_store_s3.S3BlobStore`

    Raises:
        ValueError: for unknown engine values.
    """
    engine = settings.BLOB_STORE_ENGINE
    if engine == "local":
        from ypl.backend.utils.blob_store_local import LocalBlobStore

        return LocalBlobStore(
            root=settings.BLOB_STORE_LOCAL_DIR,
            base_url=settings.BLOB_STORE_LOCAL_BASE_URL,
        )
    if engine == "gcs":
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        return GCSBlobStore(bucket=settings.GCS_BUCKET_NAME)
    if engine == "s3":
        from ypl.backend.utils.blob_store_s3 import S3BlobStore

        return S3BlobStore(
            bucket=settings.S3_BUCKET_NAME,
            region=settings.S3_REGION or None,
            endpoint_url=settings.S3_ENDPOINT_URL or None,
        )
    raise ValueError(f"Unknown BLOB_STORE_ENGINE: {engine!r}. Expected 'local', 'gcs', or 's3'.")
