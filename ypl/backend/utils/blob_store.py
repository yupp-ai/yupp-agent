"""BlobStore — pluggable blob-storage abstraction.

Logical path convention (enforced by callers, not this module):
    pastes/{uuid[:2]}/{uuid}/{filename}

Usage::

    from ypl.backend.utils.blob_store import get_blob_store

    store = get_blob_store()
    await store.upload("pastes/ab/abcd.../report.txt", data, "text/plain")
    url   = await store.get_access_url("pastes/ab/abcd.../report.txt")
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

    Reads ``settings.BLOB_STORE_BACKEND`` to choose the implementation:
    - ``"local"`` → :class:`~ypl.backend.utils.blob_store_local.LocalBlobStore`
    - ``"gcs"``   → :class:`~ypl.backend.utils.blob_store_gcs.GCSBlobStore`

    Raises:
        ValueError: for unknown backend names.
    """
    backend = settings.BLOB_STORE_BACKEND
    if backend == "local":
        from ypl.backend.utils.blob_store_local import LocalBlobStore

        return LocalBlobStore(
            root=settings.BLOB_STORE_LOCAL_ROOT,
            base_url=settings.BLOB_STORE_LOCAL_BASE_URL,
        )
    if backend == "gcs":
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        return GCSBlobStore(bucket=settings.GCS_BUCKET_NAME)
    raise ValueError(f"Unknown BLOB_STORE_BACKEND: {backend!r}. Expected 'local' or 'gcs'.")
