"""GCSBlobStore — Google Cloud Storage-backed BlobStore implementation.

Wraps the existing :mod:`ypl.backend.utils.gcs_utils` helpers for upload,
download, and signed-URL generation, and uses ``gcloud.aio.storage`` directly
for ``exists``, ``delete``, and ``get_size`` (metadata-only operations that do
not have a dedicated helper in gcs_utils).

Objects are stored as ``gs://<bucket>/<path>``.
"""

from __future__ import annotations
from typing import cast

import aiohttp
from gcloud.aio.storage import Storage

from ypl.backend.utils.gcs_utils import download_from_gcs, get_signed_url, upload_to_gcs


class GCSBlobStore:
    """GCS-backed blob store."""

    def __init__(self, bucket: str) -> None:
        """
        Args:
            bucket: GCS bucket name (without ``gs://`` prefix).
        """
        self.bucket = bucket

    def _gcs_url(self, path: str) -> str:
        return f"gs://{self.bucket}/{path}"

    # ------------------------------------------------------------------
    # BlobStore protocol
    # ------------------------------------------------------------------

    async def upload(
        self,
        path: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        await upload_to_gcs(data, self._gcs_url(path), content_type)

    async def download(self, path: str) -> bytes:
        return await download_from_gcs(self._gcs_url(path))

    async def get_size(self, path: str) -> int:
        async with aiohttp.ClientSession() as session, Storage(session=session) as storage:  # type: ignore[arg-type]
            bucket = storage.get_bucket(self.bucket)
            try:
                blob = await bucket.get_blob(path)
            except aiohttp.ClientResponseError as exc:
                if exc.status == 404:
                    raise FileNotFoundError(f"Blob not found: {path!r}") from exc
                raise
        if blob is None:
            raise FileNotFoundError(f"Blob not found: {path!r}")
        if blob.size is None:
            raise ValueError(f"GCS blob has no size metadata: {path!r}")
        return int(blob.size)

    async def get_access_url(self, path: str, expiry_seconds: int = 3 * 24 * 3600) -> str:
        # expiry_seconds is accepted for Protocol conformance but is best-effort:
        # get_signed_url hard-codes a fixed TTL internally. Callers requiring a
        # precise expiry should call get_signed_url directly.
        # async_timed_cache loses the return type annotation; cast restores it.
        return cast(str, await get_signed_url(self._gcs_url(path)))

    async def exists(self, path: str) -> bool:
        try:
            async with aiohttp.ClientSession() as session, Storage(session=session) as storage:  # type: ignore[arg-type]
                bucket = storage.get_bucket(self.bucket)
                await bucket.get_blob(path)
            return True
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                return False
            raise

    async def delete(self, path: str) -> None:
        async with aiohttp.ClientSession() as session, Storage(session=session) as storage:  # type: ignore[arg-type]
            try:
                await storage.delete(self.bucket, path)
            except aiohttp.ClientResponseError as exc:
                if exc.status == 404:
                    raise FileNotFoundError(f"Blob not found: {path!r}") from exc
                raise
