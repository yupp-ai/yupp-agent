"""S3BlobStore — Amazon S3-backed BlobStore implementation.

Uses ``aioboto3`` for asynchronous S3 access. Mirrors the
:class:`~ypl.backend.utils.blob_store_gcs.GCSBlobStore` shape with methods
``upload``, ``download``, ``get_size``, ``get_access_url`` (presigned),
``exists``, and ``delete``.

Objects are stored at ``s3://<bucket>/<path>``.

The optional ``endpoint_url`` argument lets callers point the store at a
non-AWS S3-compatible endpoint such as MinIO or LocalStack — this is what
the test suite uses via ``moto`` and what local docker-compose stacks use
for an ephemeral object store.
"""

from __future__ import annotations
from typing import TYPE_CHECKING, Any, cast

import aioboto3
from botocore.exceptions import ClientError

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from types_aiobotocore_s3.client import S3Client


# 404-class error codes returned by S3 / S3-compatible servers when a key
# does not exist. Different implementations use slightly different codes,
# so we accept the union.
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "404", "NotFound"})


def _is_not_found(exc: ClientError) -> bool:
    """Return True if *exc* describes an S3 "object not found" condition."""
    error = exc.response.get("Error", {}) if hasattr(exc, "response") else {}
    code = str(error.get("Code", ""))
    if code in _NOT_FOUND_CODES:
        return True
    # botocore stores HTTP status under ResponseMetadata
    metadata = exc.response.get("ResponseMetadata", {}) if hasattr(exc, "response") else {}
    return int(metadata.get("HTTPStatusCode", 0)) == 404


class S3BlobStore:
    """S3-backed blob store using ``aioboto3``."""

    def __init__(
        self,
        bucket: str,
        region: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        """
        Args:
            bucket: S3 bucket name (without ``s3://`` prefix).
            region: AWS region (e.g. ``us-west-2``). Required for AWS S3;
                ignored by some S3-compatible servers (MinIO).
            endpoint_url: Optional override for the S3 endpoint. Set this
                to e.g. ``http://minio:9000`` for MinIO or ``http://localhost:4566``
                for LocalStack. Leave unset for real AWS S3.
        """
        self.bucket = bucket
        self.region = region
        self.endpoint_url = endpoint_url
        # aioboto3.Session is cheap to construct; per-call clients are async
        # context managers. We hold the session here and create clients on
        # demand inside each method.
        self._session = aioboto3.Session()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _client(self) -> Any:
        """Open a new ``aioboto3`` S3 client.

        Returns an async context manager. Callers must use
        ``async with self._client() as s3:``.
        """
        kwargs: dict[str, Any] = {}
        if self.region is not None:
            kwargs["region_name"] = self.region
        if self.endpoint_url is not None:
            kwargs["endpoint_url"] = self.endpoint_url
        return self._session.client("s3", **kwargs)

    # ------------------------------------------------------------------
    # BlobStore protocol
    # ------------------------------------------------------------------

    async def upload(
        self,
        path: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        async with self._client() as s3:
            s3 = cast("S3Client", s3)
            await s3.put_object(
                Bucket=self.bucket,
                Key=path,
                Body=data,
                ContentType=content_type,
            )

    async def download(self, path: str) -> bytes:
        async with self._client() as s3:
            s3 = cast("S3Client", s3)
            try:
                resp = await s3.get_object(Bucket=self.bucket, Key=path)
            except ClientError as exc:
                if _is_not_found(exc):
                    raise FileNotFoundError(f"Blob not found: {path!r}") from exc
                raise
            body = resp["Body"]
            try:
                data = await body.read()
            finally:
                # aiobotocore's StreamingBody must be closed to release the
                # underlying connection back to the pool.
                close = getattr(body, "close", None)
                if close is not None:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
            return cast(bytes, data)

    async def get_size(self, path: str) -> int:
        async with self._client() as s3:
            s3 = cast("S3Client", s3)
            try:
                head = await s3.head_object(Bucket=self.bucket, Key=path)
            except ClientError as exc:
                if _is_not_found(exc):
                    raise FileNotFoundError(f"Blob not found: {path!r}") from exc
                raise
            length = head.get("ContentLength")
            if length is None:
                raise ValueError(f"S3 object has no ContentLength metadata: {path!r}")
            return int(length)

    async def get_access_url(self, path: str, expiry_seconds: int = 3 * 24 * 3600) -> str:
        async with self._client() as s3:
            s3 = cast("S3Client", s3)
            url = await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": path},
                ExpiresIn=expiry_seconds,
            )
            return cast(str, url)

    async def exists(self, path: str) -> bool:
        async with self._client() as s3:
            s3 = cast("S3Client", s3)
            try:
                await s3.head_object(Bucket=self.bucket, Key=path)
            except ClientError as exc:
                if _is_not_found(exc):
                    return False
                raise
            return True

    async def delete(self, path: str) -> None:
        # S3's DeleteObject is idempotent — it returns 204 even when the key
        # is missing, so we have to probe with head_object first to honour the
        # FileNotFoundError contract.
        async with self._client() as s3:
            s3 = cast("S3Client", s3)
            try:
                await s3.head_object(Bucket=self.bucket, Key=path)
            except ClientError as exc:
                if _is_not_found(exc):
                    raise FileNotFoundError(f"Blob not found: {path!r}") from exc
                raise
            await s3.delete_object(Bucket=self.bucket, Key=path)
