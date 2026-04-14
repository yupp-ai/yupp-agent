"""LocalBlobStore — filesystem-backed BlobStore implementation.

Objects are stored under a configurable root directory.  The logical path
maps directly to a relative file path beneath that root, e.g.::

    root=/data/artifacts
    path=pastes/ab/abcd.../report.txt
    → /data/artifacts/pastes/ab/abcd.../report.txt

A path-traversal guard prevents callers from escaping the root directory.
"""

from __future__ import annotations
import asyncio
import pathlib


class LocalBlobStore:
    """Filesystem-backed blob store."""

    def __init__(self, root: str, base_url: str = "") -> None:
        """
        Args:
            root: Absolute (or relative) path to the storage root directory.
                  The directory is created on first use if it does not exist.
            base_url: Base URL prepended when generating access URLs, e.g.
                      ``"https://artifacts.example.com"``.  A trailing slash
                      is stripped automatically.  If empty, ``get_access_url``
                      raises :class:`ValueError`.
        """
        self._root = pathlib.Path(root).resolve()
        self._base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _full_path(self, path: str) -> pathlib.Path:
        """Resolve *path* relative to the storage root.

        Raises:
            ValueError: if the resolved path would escape the root directory.
        """
        full = (self._root / path).resolve()
        try:
            full.relative_to(self._root)
        except ValueError as err:
            raise ValueError(f"Path {path!r} escapes the storage root {self._root}") from err
        return full

    # ------------------------------------------------------------------
    # BlobStore protocol
    # ------------------------------------------------------------------

    async def upload(
        self,
        path: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        full = self._full_path(path)
        await asyncio.to_thread(full.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(full.write_bytes, data)

    async def download(self, path: str) -> bytes:
        full = self._full_path(path)
        if not await asyncio.to_thread(full.exists):
            raise FileNotFoundError(f"Blob not found: {path!r}")
        return await asyncio.to_thread(full.read_bytes)

    async def get_size(self, path: str) -> int:
        full = self._full_path(path)
        if not await asyncio.to_thread(full.exists):
            raise FileNotFoundError(f"Blob not found: {path!r}")
        stat = await asyncio.to_thread(full.stat)
        return stat.st_size

    async def get_access_url(self, path: str, expiry_seconds: int = 3 * 24 * 3600) -> str:
        if not self._base_url:
            raise ValueError(
                "BLOB_STORE_LOCAL_BASE_URL is not configured. "
                "Set it to a base URL (e.g. https://artifacts.example.com) "
                "to generate access URLs for LocalBlobStore."
            )
        return f"{self._base_url}/{path}"

    async def exists(self, path: str) -> bool:
        return await asyncio.to_thread(self._full_path(path).exists)

    async def delete(self, path: str) -> None:
        full = self._full_path(path)
        if not await asyncio.to_thread(full.exists):
            raise FileNotFoundError(f"Blob not found: {path!r}")
        await asyncio.to_thread(full.unlink)
