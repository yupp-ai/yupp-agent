"""Unit tests for BlobStore protocol, LocalBlobStore, and GCSBlobStore.

LocalBlobStore tests use pytest's ``tmp_path`` fixture for an isolated
filesystem.  GCSBlobStore tests mock ``gcs_utils`` and ``gcloud.aio.storage``
so no real GCS credentials are required.

Note: asyncio_mode = "auto" — no @pytest.mark.asyncio needed.
"""

from __future__ import annotations
import pathlib
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
        assert isinstance(store, GCSBlobStore)


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


# ---------------------------------------------------------------------------
# get_blob_store factory
# ---------------------------------------------------------------------------


class TestGetBlobStore:
    def test_returns_local_store_by_default(self, tmp_path: pathlib.Path) -> None:
        with patch("ypl.backend.utils.blob_store.settings") as mock_settings:
            mock_settings.BLOB_STORE_BACKEND = "local"
            mock_settings.BLOB_STORE_LOCAL_ROOT = str(tmp_path)
            mock_settings.BLOB_STORE_LOCAL_BASE_URL = "https://example.com"
            store = get_blob_store()
        assert isinstance(store, LocalBlobStore)

    def test_returns_gcs_store_when_configured(self) -> None:
        from ypl.backend.utils.blob_store_gcs import GCSBlobStore

        with patch("ypl.backend.utils.blob_store.settings") as mock_settings:
            mock_settings.BLOB_STORE_BACKEND = "gcs"
            mock_settings.GCS_BUCKET_NAME = "my-bucket"
            store = get_blob_store()
        assert isinstance(store, GCSBlobStore)
        assert store.bucket == "my-bucket"

    def test_raises_on_unknown_backend(self) -> None:
        with patch("ypl.backend.utils.blob_store.settings") as mock_settings:
            mock_settings.BLOB_STORE_BACKEND = "s3"
            with pytest.raises(ValueError, match="Unknown BLOB_STORE_BACKEND"):
                get_blob_store()
