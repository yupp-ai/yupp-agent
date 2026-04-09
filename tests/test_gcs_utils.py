"""Unit tests for backend utils/gcs_utils.py.

Covers:
- GCSUrl.from_url (valid / invalid schemes / path stripping)
- get_authenticated_url (file / directory / non-GCS)
- download_from_gcs (mocked GCS storage)
- upload_to_gcs (mocked GCS storage)
"""

from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.backend.utils.gcs_utils import GCSUrl, get_authenticated_url

# ---------------------------------------------------------------------------
# GCSUrl.from_url
# ---------------------------------------------------------------------------


class TestGCSUrlFromUrl:
    def test_basic_url(self) -> None:
        url = GCSUrl.from_url("gs://my-bucket/path/to/object.txt")
        assert url.bucket == "my-bucket"
        assert url.object_path == "path/to/object.txt"

    def test_leading_slash_stripped(self) -> None:
        url = GCSUrl.from_url("gs://bucket//path/file")
        assert url.bucket == "bucket"
        assert url.object_path == "path/file"

    def test_root_object(self) -> None:
        url = GCSUrl.from_url("gs://bucket/file.txt")
        assert url.bucket == "bucket"
        assert url.object_path == "file.txt"

    def test_raises_on_non_gs_scheme(self) -> None:
        with pytest.raises(ValueError, match="Not a gcs url"):
            GCSUrl.from_url("https://example.com/file")

    def test_raises_on_s3_scheme(self) -> None:
        with pytest.raises(ValueError, match="Not a gcs url"):
            GCSUrl.from_url("s3://bucket/key")

    def test_deep_path(self) -> None:
        url = GCSUrl.from_url("gs://my-bucket/a/b/c/d/e.txt")
        assert url.object_path == "a/b/c/d/e.txt"

    def test_bucket_with_hyphens(self) -> None:
        url = GCSUrl.from_url("gs://my-cool-bucket/object")
        assert url.bucket == "my-cool-bucket"


# ---------------------------------------------------------------------------
# get_authenticated_url
# ---------------------------------------------------------------------------


class TestGetAuthenticatedUrl:
    def test_file_returns_storage_cloud_url(self) -> None:
        mock_path = MagicMock()
        mock_path.as_url.return_value = "https://storage.googleapis.com/my-bucket/my-file.txt"
        mock_path.is_dir.return_value = False

        result = get_authenticated_url(mock_path)
        assert result.startswith("https://storage.cloud.google.com/")
        assert "my-bucket/my-file.txt" in result

    def test_directory_returns_console_url(self) -> None:
        mock_path = MagicMock()
        mock_path.as_url.return_value = "https://storage.googleapis.com/my-bucket/my-dir/"
        mock_path.is_dir.return_value = True

        result = get_authenticated_url(mock_path)
        assert result.startswith("https://console.cloud.google.com/storage/browser/")
        assert "my-bucket/my-dir/" in result

    def test_non_gcs_url_raises(self) -> None:
        mock_path = MagicMock()
        mock_path.as_url.return_value = "https://example.com/file.txt"
        mock_path.is_dir.return_value = False

        with pytest.raises(ValueError, match="Not a public GCS URL"):
            get_authenticated_url(mock_path)


# ---------------------------------------------------------------------------
# download_from_gcs
# ---------------------------------------------------------------------------


class TestDownloadFromGcs:
    @pytest.mark.asyncio
    async def test_downloads_bytes(self) -> None:
        from ypl.backend.utils.gcs_utils import download_from_gcs

        mock_storage = AsyncMock()
        mock_storage.__aenter__ = AsyncMock(return_value=mock_storage)
        mock_storage.__aexit__ = AsyncMock(return_value=None)
        mock_storage.download = AsyncMock(return_value=b"file content")

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("ypl.backend.utils.gcs_utils.aiohttp.ClientSession", return_value=mock_session),
            patch("ypl.backend.utils.gcs_utils.Storage", return_value=mock_storage),
        ):
            result = await download_from_gcs("gs://my-bucket/path/to/file.txt")

        assert result == b"file content"
        mock_storage.download.assert_awaited_once_with(
            bucket="my-bucket",
            object_name="path/to/file.txt",
        )

    @pytest.mark.asyncio
    async def test_raises_on_invalid_url(self) -> None:
        from ypl.backend.utils.gcs_utils import download_from_gcs

        with pytest.raises(ValueError, match="Not a gcs url"):
            await download_from_gcs("https://not-a-gcs-url.com/file")


# ---------------------------------------------------------------------------
# upload_to_gcs
# ---------------------------------------------------------------------------


class TestUploadToGcs:
    @pytest.mark.asyncio
    async def test_uploads_bytes(self) -> None:
        from ypl.backend.utils.gcs_utils import upload_to_gcs

        mock_storage = AsyncMock()
        mock_storage.__aenter__ = AsyncMock(return_value=mock_storage)
        mock_storage.__aexit__ = AsyncMock(return_value=None)
        mock_storage.upload = AsyncMock(return_value=None)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("ypl.backend.utils.gcs_utils.aiohttp.ClientSession", return_value=mock_session),
            patch("ypl.backend.utils.gcs_utils.Storage", return_value=mock_storage),
        ):
            await upload_to_gcs(b"some bytes", "gs://bucket/path/to/file.txt", content_type="text/plain")

        mock_storage.upload.assert_awaited_once_with(
            bucket="bucket",
            object_name="path/to/file.txt",
            file_data=b"some bytes",
            headers={"Content-Type": "text/plain"},
        )

    @pytest.mark.asyncio
    async def test_default_content_type(self) -> None:
        from ypl.backend.utils.gcs_utils import upload_to_gcs

        mock_storage = AsyncMock()
        mock_storage.__aenter__ = AsyncMock(return_value=mock_storage)
        mock_storage.__aexit__ = AsyncMock(return_value=None)
        mock_storage.upload = AsyncMock(return_value=None)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("ypl.backend.utils.gcs_utils.aiohttp.ClientSession", return_value=mock_session),
            patch("ypl.backend.utils.gcs_utils.Storage", return_value=mock_storage),
        ):
            await upload_to_gcs(b"data", "gs://bucket/object")

        call_kwargs = mock_storage.upload.call_args[1]
        assert call_kwargs["headers"]["Content-Type"] == "application/octet-stream"


# ---------------------------------------------------------------------------
# copy_file
# ---------------------------------------------------------------------------


class TestCopyFile:
    @pytest.mark.asyncio
    async def test_copies_between_buckets(self) -> None:
        from ypl.backend.utils.gcs_utils import GCSUrl, copy_file

        mock_storage = AsyncMock()
        mock_storage.__aenter__ = AsyncMock(return_value=mock_storage)
        mock_storage.__aexit__ = AsyncMock(return_value=None)
        mock_storage.copy = AsyncMock(return_value=None)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        src = GCSUrl(bucket="src-bucket", object_path="src/file.txt")
        dst = GCSUrl(bucket="dst-bucket", object_path="dst/file.txt")

        with (
            patch("ypl.backend.utils.gcs_utils.aiohttp.ClientSession", return_value=mock_session),
            patch("ypl.backend.utils.gcs_utils.Storage", return_value=mock_storage),
        ):
            await copy_file(src, dst)

        mock_storage.copy.assert_awaited_once_with(
            bucket="src-bucket",
            object_name="src/file.txt",
            destination_bucket="dst-bucket",
            new_name="dst/file.txt",
        )
