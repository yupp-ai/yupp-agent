import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp
import httpx
from cloudpathlib.cloudpath import CloudPath
from gcloud.aio.storage import Storage
from tenacity import after_log, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ypl.structured_logger import get_logger
from ypl.utils import async_timed_cache

logger = get_logger()

GS_SIGNED_URL_CACHE_TTL_SECONDS = 3 * 24 * 60 * 60  # 3 days

# Retry configuration for GCS operations
GCS_RETRY_ATTEMPTS = 3
GCS_RETRY_WAIT_MULTIPLIER = 2
GCS_RETRY_WAIT_MIN = 1
GCS_RETRY_WAIT_MAX = 10

# Retry decorator for GCS operations
retry_gcs = retry(
    stop=stop_after_attempt(GCS_RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=GCS_RETRY_WAIT_MULTIPLIER, min=GCS_RETRY_WAIT_MIN, max=GCS_RETRY_WAIT_MAX),
    retry=retry_if_exception_type(
        (
            TimeoutError,
            asyncio.CancelledError,
            aiohttp.ClientError,
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadTimeout,
            OSError,
        )
    ),
    after=after_log(logging.getLogger(), logging.WARNING),
    reraise=True,
)


@dataclass
class GCSUrl:
    bucket: str
    object_path: str

    @classmethod
    def from_url(cls, gcs_url: str) -> "GCSUrl":
        """
        Parse a GCS URL and returns bucket name and object path.
        E.g. gs://bucket/path/to/object -> GCSUrl("bucket", "path/to/object")
        Throws ValueError if the URL is not a valid GCS URL.
        """
        parsed = urlparse(gcs_url)
        if parsed.scheme != "gs":
            raise ValueError("Not a gcs url")

        bucket = parsed.netloc
        object_path = parsed.path.lstrip("/")

        return GCSUrl(bucket=bucket, object_path=object_path)


@retry_gcs
async def download_from_gcs(url: str) -> bytes:
    gcs_url = GCSUrl.from_url(url)
    # Create aiohttp session first and pass it to Storage to avoid file descriptor conflicts
    # between uvloop and aiohttp when multiple sessions are created. See:
    # https://github.com/MagicStack/uvloop/issues/653
    async with aiohttp.ClientSession() as session, Storage(session=session) as async_client:
        return await async_client.download(
            bucket=gcs_url.bucket,
            object_name=gcs_url.object_path,
        )


_GCP_SERVICE_ACCOUNT_EMAIL = ""
_GCE_METADATA_URL_FOR_SERVICE_ACCOUNT_EMAIL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email"
)


@retry_gcs
async def get_service_account_email() -> str:
    """
    Get the service account email for from GCP credentials or GCE metadata server. This email is needed
    on GCE machines in order to run get_signed_url() API.
    """
    global _GCP_SERVICE_ACCOUNT_EMAIL
    if not _GCP_SERVICE_ACCOUNT_EMAIL:
        # First try to get from default authentication. This would succeed in local dev.
        # Create aiohttp session first and pass it to Storage to avoid file descriptor conflicts
        # between uvloop and aiohttp when multiple sessions are created. See:
        # https://github.com/MagicStack/uvloop/issues/653
        async with aiohttp.ClientSession() as session, Storage(session=session) as storage:
            if "client_email" in storage.token.service_data:
                _GCP_SERVICE_ACCOUNT_EMAIL = storage.token.service_data["client_email"]
            else:
                # If that fails, try to get from metadata server. This would succeed on GCE.
                try:
                    async with httpx.AsyncClient() as client:
                        response = await client.get(
                            _GCE_METADATA_URL_FOR_SERVICE_ACCOUNT_EMAIL, headers={"Metadata-Flavor": "Google"}
                        )
                        _GCP_SERVICE_ACCOUNT_EMAIL = response.text
                except Exception as e:
                    logger.error({"message": "Failed to get GCP service account email"}, exc_info=True)
                    raise e

        logger.info({"message": f"GCP service account email is {_GCP_SERVICE_ACCOUNT_EMAIL}"})

    return _GCP_SERVICE_ACCOUNT_EMAIL


@async_timed_cache(seconds=6 * 60 * 60)  # 6 hour cache, its an LRU cache 128 items. Not too costly.
@retry_gcs
async def get_signed_url(full_gcs_url: str) -> str:
    """
    Get a signed URL for a GCS object. The signed URL is valid for 3 days. It is cached for 6 hours.
    """
    gcs_url = GCSUrl.from_url(full_gcs_url)
    # Fetch service account email before opening sessions to avoid overlapping Storage clients,
    # since get_service_account_email() creates its own Storage client when the cache is empty.
    service_account_email = await get_service_account_email()
    # Create aiohttp session first and pass it to Storage to avoid file descriptor conflicts
    # between uvloop and aiohttp when multiple sessions are created. See:
    # https://github.com/MagicStack/uvloop/issues/653
    async with (
        aiohttp.ClientSession() as session,
        Storage(session=session) as async_client,
    ):
        bucket = async_client.get_bucket(gcs_url.bucket)
        blob = await bucket.get_blob(gcs_url.object_path)
        return await blob.get_signed_url(
            expiration=GS_SIGNED_URL_CACHE_TTL_SECONDS,
            service_account_email=service_account_email,
            session=session,
        )


@retry_gcs
async def upload_to_gcs(data: bytes, gcs_url: str, content_type: str = "application/octet-stream") -> None:
    """Upload bytes to a GCS object.

    Args:
        data: Raw bytes to upload.
        gcs_url: Target GCS URL (e.g., gs://bucket/path/to/object).
        content_type: MIME type for the uploaded object.
    """
    parsed = GCSUrl.from_url(gcs_url)
    async with aiohttp.ClientSession() as session, Storage(session=session) as async_client:
        await async_client.upload(
            bucket=parsed.bucket,
            object_name=parsed.object_path,
            file_data=data,
            headers={"Content-Type": content_type},
        )


@retry_gcs
async def copy_file(source: GCSUrl, destination: GCSUrl) -> None:
    # Create aiohttp session first and pass it to Storage to avoid file descriptor conflicts
    # between uvloop and aiohttp when multiple sessions are created. See:
    # https://github.com/MagicStack/uvloop/issues/653
    async with aiohttp.ClientSession() as session, Storage(session=session) as async_client:
        await async_client.copy(
            bucket=source.bucket,
            object_name=source.object_path,
            destination_bucket=destination.bucket,
            new_name=destination.object_path,
        )


def get_authenticated_url(gcs_path: CloudPath) -> str:
    public_url = gcs_path.as_url()
    if public_url.startswith("https://storage.googleapis.com/"):
        if gcs_path.is_dir():
            authenticated_url = public_url.replace(
                "https://storage.googleapis.com/", "https://console.cloud.google.com/storage/browser/"
            )
        else:
            authenticated_url = public_url.replace(
                "https://storage.googleapis.com/", "https://storage.cloud.google.com/"
            )
        return authenticated_url
    raise ValueError(f"Not a public GCS URL: {public_url}")
