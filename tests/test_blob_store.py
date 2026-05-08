"""Unit tests for BlobStore protocol, LocalBlobStore, and GCSBlobStore.

LocalBlobStore tests use pytest's ``tmp_path`` fixture for an isolated
filesystem.  GCSBlobStore tests mock ``gcs_utils`` and ``gcloud.aio.storage``
so no real GCS credentials are required.

Note: asyncio_mode = "auto" — no @pytest.mark.asyncio needed.
"""

from __future__ import annotations
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.backend.utils.blob_store import BlobStore, get_blob_store
from ypl.backend.utils.blob_store_local import LocalBlobStore

# ---------------------------------------------------------------------------
# Shared fixtures / constants
# ---------------------------------------------------------------------------

SAMPLE_PATH = "pastes/ab/abcd1234-ef56-7890-abcd-ef1234567890/report.txt"
SAMPLE_DATA = b"Hello, BlobStore!"
SAMPLE_CONTENT_TYPE = "text/plain"


@pytest.fixture()
def local_store(tmp_path: pathlib.Path) -> LocalBlobStore:
    """Return a LocalBlobStore rooted at a temporary directory."""
    return LocalBlobStore(root=str(tmp_path), base_url="https://artifacts.example.com")


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestBlobStoreProtocol:
    """Verify that both concrete implementations satisfy the BlobStore Protocol."""

    def test_local_store_is_blob_store(self, tmp_path: pathlib.Path) -> None:
        store = LocalBlobStore(root=str(tmp_path))
        assert isinstance(store, BlobStore)

    def test_gcs_store_is_blob_store(self) -> None:
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        store = GCSBlobStore(bucket="my-bucket")
        assert isinstance(store, BlobStore)


# ---------------------------------------------------------------------------
# LocalBlobStore — upload / download
# ---------------------------------------------------------------------------


class TestLocalBlobStoreUploadDownload:
    async def test_upload_creates_file(self, local_store: LocalBlobStore, tmp_path: pathlib.Path) -> None:
        await local_store.upload(SAMPLE_PATH, SAMPLE_DATA, SAMPLE_CONTENT_TYPE)
        full = local_store._full_path(SAMPLE_PATH)
        assert full.exists()
        assert full.read_bytes() == SAMPLE_DATA

    async def test_upload_creates_parent_dirs(self, local_store: LocalBlobStore) -> None:
        nested = "pastes/xx/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/nested/deeply/file.bin"
        await local_store.upload(nested, b"data")
        assert local_store._full_path(nested).exists()

    async def test_download_returns_data(self, local_store: LocalBlobStore) -> None:
        await local_store.upload(SAMPLE_PATH, SAMPLE_DATA)
        result = await local_store.download(SAMPLE_PATH)
        assert result == SAMPLE_DATA

    async def test_download_missing_raises(self, local_store: LocalBlobStore) -> None:
        with pytest.raises(FileNotFoundError):
            await local_store.download("pastes/zz/missing/file.txt")

    async def test_upload_overwrites(self, local_store: LocalBlobStore) -> None:
        await local_store.upload(SAMPLE_PATH, b"original")
        await local_store.upload(SAMPLE_PATH, b"updated")
        assert await local_store.download(SAMPLE_PATH) == b"updated"


# ---------------------------------------------------------------------------
# LocalBlobStore — get_size
# ---------------------------------------------------------------------------


class TestLocalBlobStoreGetSize:
    async def test_get_size_after_upload(self, local_store: LocalBlobStore) -> None:
        await local_store.upload(SAMPLE_PATH, SAMPLE_DATA)
        size = await local_store.get_size(SAMPLE_PATH)
        assert size == len(SAMPLE_DATA)

    async def test_get_size_missing_raises(self, local_store: LocalBlobStore) -> None:
        with pytest.raises(FileNotFoundError):
            await local_store.get_size("pastes/zz/missing/file.txt")


# ---------------------------------------------------------------------------
# LocalBlobStore — get_access_url
# ---------------------------------------------------------------------------


class TestLocalBlobStoreGetAccessUrl:
    async def test_returns_url_with_base(self, local_store: LocalBlobStore) -> None:
        url = await local_store.get_access_url(SAMPLE_PATH)
        assert url == f"https://artifacts.example.com/{SAMPLE_PATH}"

    async def test_strips_trailing_slash_from_base_url(self, tmp_path: pathlib.Path) -> None:
        store = LocalBlobStore(root=str(tmp_path), base_url="https://artifacts.example.com/")
        url = await store.get_access_url(SAMPLE_PATH)
        assert url == f"https://artifacts.example.com/{SAMPLE_PATH}"

    async def test_raises_without_base_url(self, tmp_path: pathlib.Path) -> None:
        store = LocalBlobStore(root=str(tmp_path))  # no base_url
        with pytest.raises(ValueError, match="BLOB_STORE_LOCAL_BASE_URL"):
            await store.get_access_url(SAMPLE_PATH)


# ---------------------------------------------------------------------------
# LocalBlobStore — exists / delete
# ---------------------------------------------------------------------------


class TestLocalBlobStoreExistsDelete:
    async def test_exists_false_before_upload(self, local_store: LocalBlobStore) -> None:
        assert not await local_store.exists(SAMPLE_PATH)

    async def test_exists_true_after_upload(self, local_store: LocalBlobStore) -> None:
        await local_store.upload(SAMPLE_PATH, SAMPLE_DATA)
        assert await local_store.exists(SAMPLE_PATH)

    async def test_delete_removes_file(self, local_store: LocalBlobStore) -> None:
        await local_store.upload(SAMPLE_PATH, SAMPLE_DATA)
        await local_store.delete(SAMPLE_PATH)
        assert not await local_store.exists(SAMPLE_PATH)

    async def test_delete_missing_raises(self, local_store: LocalBlobStore) -> None:
        with pytest.raises(FileNotFoundError):
            await local_store.delete(SAMPLE_PATH)

    async def test_exists_false_after_delete(self, local_store: LocalBlobStore) -> None:
        await local_store.upload(SAMPLE_PATH, SAMPLE_DATA)
        await local_store.delete(SAMPLE_PATH)
        assert not await local_store.exists(SAMPLE_PATH)


# ---------------------------------------------------------------------------
# LocalBlobStore — path traversal protection
# ---------------------------------------------------------------------------


class TestLocalBlobStorePathTraversal:
    def test_traversal_raises(self, local_store: LocalBlobStore) -> None:
        with pytest.raises(ValueError, match="escapes the storage root"):
            local_store._full_path("../../etc/passwd")


# ---------------------------------------------------------------------------
# GCSBlobStore — unit tests (mocked GCS)
# ---------------------------------------------------------------------------


class TestGCSBlobStoreUpload:
    async def test_upload_calls_gcs_utils(self) -> None:
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        store = GCSBlobStore(bucket="test-bucket")
        with patch(
            "ypl.backend.utils.blob_store_gcs.upload_to_gcs",
            new_callable=AsyncMock,
        ) as mock_upload:
            await store.upload(SAMPLE_PATH, SAMPLE_DATA, SAMPLE_CONTENT_TYPE)
            mock_upload.assert_awaited_once_with(
                SAMPLE_DATA,
                f"gs://test-bucket/{SAMPLE_PATH}",
                SAMPLE_CONTENT_TYPE,
            )


class TestGCSBlobStoreDownload:
    async def test_download_calls_gcs_utils(self) -> None:
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        store = GCSBlobStore(bucket="test-bucket")
        with patch(
            "ypl.backend.utils.blob_store_gcs.download_from_gcs",
            new_callable=AsyncMock,
            return_value=SAMPLE_DATA,
        ) as mock_download:
            result = await store.download(SAMPLE_PATH)
            assert result == SAMPLE_DATA
            mock_download.assert_awaited_once_with(f"gs://test-bucket/{SAMPLE_PATH}")


class TestGCSBlobStoreGetAccessUrl:
    async def test_returns_signed_url(self) -> None:
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        store = GCSBlobStore(bucket="test-bucket")
        signed_url = "https://storage.googleapis.com/signed?token=abc"
        with patch(
            "ypl.backend.utils.blob_store_gcs.get_signed_url",
            new_callable=AsyncMock,
            return_value=signed_url,
        ) as mock_sign:
            result = await store.get_access_url(SAMPLE_PATH)
            assert result == signed_url
            mock_sign.assert_awaited_once_with(f"gs://test-bucket/{SAMPLE_PATH}")


class TestGCSBlobStoreGetSize:
    async def test_get_size_returns_blob_size(self) -> None:
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        store = GCSBlobStore(bucket="test-bucket")

        mock_blob = MagicMock()
        mock_blob.size = "42"
        mock_bucket = MagicMock()
        mock_bucket.get_blob = AsyncMock(return_value=mock_blob)
        mock_storage = MagicMock()
        mock_storage.get_bucket = MagicMock(return_value=mock_bucket)
        mock_storage.__aenter__ = AsyncMock(return_value=mock_storage)
        mock_storage.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.backend.utils.blob_store_gcs.aiohttp.ClientSession", return_value=mock_session),
            patch("ypl.backend.utils.blob_store_gcs.Storage", return_value=mock_storage),
        ):
            size = await store.get_size(SAMPLE_PATH)
        assert size == 42

    async def test_get_size_raises_on_404(self) -> None:
        import aiohttp as _aiohttp
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        store = GCSBlobStore(bucket="test-bucket")
        not_found = _aiohttp.ClientResponseError(request_info=MagicMock(), history=(), status=404)
        mock_bucket = MagicMock()
        mock_bucket.get_blob = AsyncMock(side_effect=not_found)
        mock_storage = MagicMock()
        mock_storage.get_bucket = MagicMock(return_value=mock_bucket)
        mock_storage.__aenter__ = AsyncMock(return_value=mock_storage)
        mock_storage.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.backend.utils.blob_store_gcs.aiohttp.ClientSession", return_value=mock_session),
            patch("ypl.backend.utils.blob_store_gcs.Storage", return_value=mock_storage),
            pytest.raises(FileNotFoundError),
        ):
            await store.get_size(SAMPLE_PATH)

    async def test_get_size_raises_when_blob_is_none(self) -> None:
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        store = GCSBlobStore(bucket="test-bucket")
        mock_bucket = MagicMock()
        mock_bucket.get_blob = AsyncMock(return_value=None)
        mock_storage = MagicMock()
        mock_storage.get_bucket = MagicMock(return_value=mock_bucket)
        mock_storage.__aenter__ = AsyncMock(return_value=mock_storage)
        mock_storage.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.backend.utils.blob_store_gcs.aiohttp.ClientSession", return_value=mock_session),
            patch("ypl.backend.utils.blob_store_gcs.Storage", return_value=mock_storage),
            pytest.raises(FileNotFoundError),
        ):
            await store.get_size(SAMPLE_PATH)

    async def test_get_size_raises_when_size_is_none(self) -> None:
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        store = GCSBlobStore(bucket="test-bucket")
        mock_blob = MagicMock()
        mock_blob.size = None
        mock_bucket = MagicMock()
        mock_bucket.get_blob = AsyncMock(return_value=mock_blob)
        mock_storage = MagicMock()
        mock_storage.get_bucket = MagicMock(return_value=mock_bucket)
        mock_storage.__aenter__ = AsyncMock(return_value=mock_storage)
        mock_storage.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.backend.utils.blob_store_gcs.aiohttp.ClientSession", return_value=mock_session),
            patch("ypl.backend.utils.blob_store_gcs.Storage", return_value=mock_storage),
            pytest.raises(ValueError, match="no size metadata"),
        ):
            await store.get_size(SAMPLE_PATH)


class TestGCSBlobStoreExists:
    async def test_exists_true_when_blob_found(self) -> None:
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        store = GCSBlobStore(bucket="test-bucket")

        mock_bucket = MagicMock()
        mock_bucket.get_blob = AsyncMock(return_value=MagicMock())
        mock_storage = MagicMock()
        mock_storage.get_bucket = MagicMock(return_value=mock_bucket)
        mock_storage.__aenter__ = AsyncMock(return_value=mock_storage)
        mock_storage.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.backend.utils.blob_store_gcs.aiohttp.ClientSession", return_value=mock_session),
            patch("ypl.backend.utils.blob_store_gcs.Storage", return_value=mock_storage),
        ):
            result = await store.exists(SAMPLE_PATH)
        assert result is True

    async def test_exists_false_when_blob_is_none(self) -> None:
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        store = GCSBlobStore(bucket="test-bucket")

        mock_bucket = MagicMock()
        mock_bucket.get_blob = AsyncMock(return_value=None)
        mock_storage = MagicMock()
        mock_storage.get_bucket = MagicMock(return_value=mock_bucket)
        mock_storage.__aenter__ = AsyncMock(return_value=mock_storage)
        mock_storage.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.backend.utils.blob_store_gcs.aiohttp.ClientSession", return_value=mock_session),
            patch("ypl.backend.utils.blob_store_gcs.Storage", return_value=mock_storage),
        ):
            result = await store.exists(SAMPLE_PATH)
        assert result is False

    async def test_exists_false_on_404(self) -> None:
        import aiohttp as _aiohttp
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        store = GCSBlobStore(bucket="test-bucket")

        not_found = _aiohttp.ClientResponseError(request_info=MagicMock(), history=(), status=404)
        mock_bucket = MagicMock()
        mock_bucket.get_blob = AsyncMock(side_effect=not_found)
        mock_storage = MagicMock()
        mock_storage.get_bucket = MagicMock(return_value=mock_bucket)
        mock_storage.__aenter__ = AsyncMock(return_value=mock_storage)
        mock_storage.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.backend.utils.blob_store_gcs.aiohttp.ClientSession", return_value=mock_session),
            patch("ypl.backend.utils.blob_store_gcs.Storage", return_value=mock_storage),
        ):
            result = await store.exists(SAMPLE_PATH)
        assert result is False


class TestGCSBlobStoreDelete:
    async def test_delete_calls_storage_delete(self) -> None:
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        store = GCSBlobStore(bucket="test-bucket")

        mock_storage = MagicMock()
        mock_storage.delete = AsyncMock(return_value=None)
        mock_storage.__aenter__ = AsyncMock(return_value=mock_storage)
        mock_storage.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.backend.utils.blob_store_gcs.aiohttp.ClientSession", return_value=mock_session),
            patch("ypl.backend.utils.blob_store_gcs.Storage", return_value=mock_storage),
        ):
            await store.delete(SAMPLE_PATH)
        mock_storage.delete.assert_awaited_once_with("test-bucket", SAMPLE_PATH)

    async def test_delete_raises_on_missing_blob(self) -> None:
        import aiohttp as _aiohttp
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        store = GCSBlobStore(bucket="test-bucket")
        not_found = _aiohttp.ClientResponseError(request_info=MagicMock(), history=(), status=404)
        mock_storage = MagicMock()
        mock_storage.delete = AsyncMock(side_effect=not_found)
        mock_storage.__aenter__ = AsyncMock(return_value=mock_storage)
        mock_storage.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.backend.utils.blob_store_gcs.aiohttp.ClientSession", return_value=mock_session),
            patch("ypl.backend.utils.blob_store_gcs.Storage", return_value=mock_storage),
            pytest.raises(FileNotFoundError),
        ):
            await store.delete(SAMPLE_PATH)


# ---------------------------------------------------------------------------
# get_blob_store factory
# ---------------------------------------------------------------------------


class TestGetBlobStore:
    def test_returns_local_store_by_default(self, tmp_path: pathlib.Path) -> None:
        with patch("ypl.backend.utils.blob_store.settings") as mock_settings:
            mock_settings.BLOB_STORE_ENGINE = "local"
            mock_settings.BLOB_STORE_LOCAL_DIR = str(tmp_path)
            mock_settings.BLOB_STORE_LOCAL_BASE_URL = "https://example.com"
            store = get_blob_store()
        assert isinstance(store, LocalBlobStore)

    def test_returns_gcs_store_when_configured(self) -> None:
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        with patch("ypl.backend.utils.blob_store.settings") as mock_settings:
            mock_settings.BLOB_STORE_ENGINE = "gcs"
            mock_settings.GCS_BUCKET_NAME = "my-bucket"
            store = get_blob_store()
        assert isinstance(store, GCSBlobStore)
        assert store.bucket == "my-bucket"

    def test_returns_s3_store_when_configured(self) -> None:
        from ypl.backend.utils.blob_store_s3 import S3BlobStore

        with patch("ypl.backend.utils.blob_store.settings") as mock_settings:
            mock_settings.BLOB_STORE_ENGINE = "s3"
            mock_settings.S3_BUCKET_NAME = "my-s3-bucket"
            mock_settings.S3_REGION = "us-west-2"
            mock_settings.S3_ENDPOINT_URL = ""
            store = get_blob_store()
        assert isinstance(store, S3BlobStore)
        assert store.bucket == "my-s3-bucket"
        assert store.region == "us-west-2"
        # Empty endpoint string in settings is normalised to ``None`` so the
        # AWS SDK's default endpoint-resolution kicks in.
        assert store.endpoint_url is None

    def test_returns_s3_store_with_minio_endpoint(self) -> None:
        from ypl.backend.utils.blob_store_s3 import S3BlobStore

        with patch("ypl.backend.utils.blob_store.settings") as mock_settings:
            mock_settings.BLOB_STORE_ENGINE = "s3"
            mock_settings.S3_BUCKET_NAME = "my-s3-bucket"
            mock_settings.S3_REGION = ""
            mock_settings.S3_ENDPOINT_URL = "http://minio:9000"
            store = get_blob_store()
        assert isinstance(store, S3BlobStore)
        # Empty region should be converted to None.
        assert store.region is None
        assert store.endpoint_url == "http://minio:9000"

    def test_raises_on_unknown_backend(self) -> None:
        with patch("ypl.backend.utils.blob_store.settings") as mock_settings:
            mock_settings.BLOB_STORE_ENGINE = "azure"
            with pytest.raises(ValueError, match="Unknown BLOB_STORE_ENGINE"):
                get_blob_store()


# ---------------------------------------------------------------------------
# S3BlobStore — unit tests (mocked aioboto3 client)
# ---------------------------------------------------------------------------


def _build_mock_s3_client() -> tuple[MagicMock, MagicMock]:
    """Build a mock aioboto3 S3 client and the surrounding context manager.

    Returns (client_mock, context_manager_mock). The client_mock has all S3
    methods patched as AsyncMock with sensible defaults — tests override the
    ones they care about.
    """
    client = MagicMock()
    client.put_object = AsyncMock(return_value={})
    client.get_object = AsyncMock()
    client.head_object = AsyncMock(return_value={"ContentLength": 0})
    client.delete_object = AsyncMock(return_value={})
    client.generate_presigned_url = AsyncMock(return_value="")

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return client, ctx


def _make_s3_store(bucket: str, ctx: MagicMock, **kwargs: Any) -> Any:
    """Build an S3BlobStore whose internal aioboto3 session is mocked.

    We can't simply patch ``aioboto3.Session`` and then construct the store
    because the store grabs a real ``Session`` in ``__init__``. Instead we
    swap ``store._session`` directly with a MagicMock whose ``.client(...)``
    method returns the supplied async context manager.
    """
    from ypl.backend.utils.blob_store_s3 import S3BlobStore

    store = S3BlobStore(bucket=bucket, **kwargs)
    fake_session = MagicMock()
    fake_session.client = MagicMock(return_value=ctx)
    store._session = fake_session
    return store


class TestS3BlobStoreUpload:
    async def test_upload_calls_put_object(self) -> None:
        client, ctx = _build_mock_s3_client()
        store = _make_s3_store("test-bucket", ctx, region="us-east-1")
        await store.upload(SAMPLE_PATH, SAMPLE_DATA, SAMPLE_CONTENT_TYPE)
        client.put_object.assert_awaited_once_with(
            Bucket="test-bucket",
            Key=SAMPLE_PATH,
            Body=SAMPLE_DATA,
            ContentType=SAMPLE_CONTENT_TYPE,
        )


class TestS3BlobStoreDownload:
    async def test_download_returns_body_bytes(self) -> None:
        client, ctx = _build_mock_s3_client()
        body = MagicMock()
        body.read = AsyncMock(return_value=SAMPLE_DATA)
        body.close = AsyncMock(return_value=None)
        client.get_object = AsyncMock(return_value={"Body": body})

        store = _make_s3_store("test-bucket", ctx)
        result = await store.download(SAMPLE_PATH)
        assert result == SAMPLE_DATA
        client.get_object.assert_awaited_once_with(Bucket="test-bucket", Key=SAMPLE_PATH)
        body.read.assert_awaited_once()
        body.close.assert_awaited_once()

    async def test_download_raises_filenotfound_on_404(self) -> None:
        from botocore.exceptions import ClientError

        client, ctx = _build_mock_s3_client()
        client.get_object = AsyncMock(
            side_effect=ClientError(
                error_response={
                    "Error": {"Code": "NoSuchKey", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                operation_name="GetObject",
            )
        )
        store = _make_s3_store("test-bucket", ctx)
        with pytest.raises(FileNotFoundError):
            await store.download(SAMPLE_PATH)

    async def test_download_propagates_other_client_errors(self) -> None:
        from botocore.exceptions import ClientError

        client, ctx = _build_mock_s3_client()
        client.get_object = AsyncMock(
            side_effect=ClientError(
                error_response={
                    "Error": {"Code": "AccessDenied", "Message": "no"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                },
                operation_name="GetObject",
            )
        )
        store = _make_s3_store("test-bucket", ctx)
        with pytest.raises(ClientError):
            await store.download(SAMPLE_PATH)


class TestS3BlobStoreGetSize:
    async def test_get_size_returns_content_length(self) -> None:
        client, ctx = _build_mock_s3_client()
        client.head_object = AsyncMock(return_value={"ContentLength": 42})
        store = _make_s3_store("test-bucket", ctx)
        size = await store.get_size(SAMPLE_PATH)
        assert size == 42

    async def test_get_size_raises_filenotfound_on_404(self) -> None:
        from botocore.exceptions import ClientError

        client, ctx = _build_mock_s3_client()
        client.head_object = AsyncMock(
            side_effect=ClientError(
                error_response={
                    "Error": {"Code": "404", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                operation_name="HeadObject",
            )
        )
        store = _make_s3_store("test-bucket", ctx)
        with pytest.raises(FileNotFoundError):
            await store.get_size(SAMPLE_PATH)

    async def test_get_size_raises_when_content_length_missing(self) -> None:
        client, ctx = _build_mock_s3_client()
        client.head_object = AsyncMock(return_value={})
        store = _make_s3_store("test-bucket", ctx)
        with pytest.raises(ValueError, match="ContentLength"):
            await store.get_size(SAMPLE_PATH)


class TestS3BlobStoreGetAccessUrl:
    async def test_returns_presigned_url(self) -> None:
        client, ctx = _build_mock_s3_client()
        client.generate_presigned_url = AsyncMock(return_value="https://s3.example.com/signed")
        store = _make_s3_store("test-bucket", ctx)
        url = await store.get_access_url(SAMPLE_PATH, expiry_seconds=900)
        assert url == "https://s3.example.com/signed"
        client.generate_presigned_url.assert_awaited_once_with(
            "get_object",
            Params={"Bucket": "test-bucket", "Key": SAMPLE_PATH},
            ExpiresIn=900,
        )


class TestS3BlobStoreExists:
    async def test_exists_true(self) -> None:
        client, ctx = _build_mock_s3_client()
        client.head_object = AsyncMock(return_value={"ContentLength": 1})
        store = _make_s3_store("test-bucket", ctx)
        assert await store.exists(SAMPLE_PATH) is True

    async def test_exists_false_on_not_found(self) -> None:
        from botocore.exceptions import ClientError

        client, ctx = _build_mock_s3_client()
        client.head_object = AsyncMock(
            side_effect=ClientError(
                error_response={
                    "Error": {"Code": "NotFound", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                operation_name="HeadObject",
            )
        )
        store = _make_s3_store("test-bucket", ctx)
        assert await store.exists(SAMPLE_PATH) is False


class TestS3BlobStoreDelete:
    async def test_delete_calls_delete_object(self) -> None:
        client, ctx = _build_mock_s3_client()
        client.head_object = AsyncMock(return_value={"ContentLength": 1})
        store = _make_s3_store("test-bucket", ctx)
        await store.delete(SAMPLE_PATH)
        client.delete_object.assert_awaited_once_with(Bucket="test-bucket", Key=SAMPLE_PATH)

    async def test_delete_raises_filenotfound_when_missing(self) -> None:
        from botocore.exceptions import ClientError

        client, ctx = _build_mock_s3_client()
        client.head_object = AsyncMock(
            side_effect=ClientError(
                error_response={
                    "Error": {"Code": "NoSuchKey", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                operation_name="HeadObject",
            )
        )
        store = _make_s3_store("test-bucket", ctx)
        with pytest.raises(FileNotFoundError):
            await store.delete(SAMPLE_PATH)
        client.delete_object.assert_not_awaited()


class TestS3BlobStoreClientKwargs:
    """Verify region and endpoint_url are forwarded to ``Session.client`` correctly."""

    async def test_region_and_endpoint_passed_to_client(self) -> None:
        client, ctx = _build_mock_s3_client()
        captured: dict[str, Any] = {}

        def _fake_client(service: str, **kwargs: Any) -> MagicMock:
            captured["service"] = service
            captured.update(kwargs)
            return ctx

        store = _make_s3_store("b", ctx, region="us-west-2", endpoint_url="http://minio:9000")
        store._session.client = MagicMock(side_effect=_fake_client)
        await store.upload(SAMPLE_PATH, SAMPLE_DATA)
        assert captured["service"] == "s3"
        assert captured["region_name"] == "us-west-2"
        assert captured["endpoint_url"] == "http://minio:9000"

    async def test_no_region_or_endpoint_omits_kwargs(self) -> None:
        client, ctx = _build_mock_s3_client()
        captured: dict[str, Any] = {}

        def _fake_client(service: str, **kwargs: Any) -> MagicMock:
            captured["service"] = service
            captured.update(kwargs)
            return ctx

        store = _make_s3_store("b", ctx)
        store._session.client = MagicMock(side_effect=_fake_client)
        await store.upload(SAMPLE_PATH, SAMPLE_DATA)
        assert captured["service"] == "s3"
        assert "region_name" not in captured
        assert "endpoint_url" not in captured
