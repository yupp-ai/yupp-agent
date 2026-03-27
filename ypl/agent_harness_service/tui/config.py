"""Connection configuration, HTTP helpers, and session file persistence."""

from __future__ import annotations
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

SESSION_FILE = Path("/tmp/LAST_AHS_SESSION_ID")
USER_ID_FILE = Path("/tmp/LAST_AHS_USER_ID")

# ---------------------------------------------------------------------------
# Config — resolved once at startup, stored as module globals
# ---------------------------------------------------------------------------

_AHS_HTTP_BASE: str = ""
_AHS_WS_BASE: str = ""
_AHS_API_KEY: str = ""


def _init_connection(host: str | None) -> None:
    """Parse --host and set module-level connection URLs.

    Accepts:
      ahs.yupp.ai          → https://ahs.yupp.ai/ahs  (wss)
      localhost:8090        → http://localhost:8090/ahs  (ws)
      http://localhost:8090 → http://localhost:8090/ahs  (ws)
    """
    global _AHS_HTTP_BASE, _AHS_WS_BASE, _AHS_API_KEY

    _AHS_API_KEY = os.environ.get("AGENT_HARNESS_SERVICE_API_KEY") or os.environ.get("AHS_API_KEY", "")
    if not _AHS_API_KEY:
        print("ERROR: AGENT_HARNESS_SERVICE_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    raw = host or os.environ.get("AHS_HOST", "ahs.yupp.ai")

    # Add scheme if missing so urlparse works
    if "://" not in raw:
        raw = f"https://{raw}"

    parsed = urlparse(raw)
    hostname = parsed.hostname or "ahs.yupp.ai"
    port = parsed.port
    scheme = parsed.scheme or "https"

    is_local = hostname in ("localhost", "127.0.0.1", "0.0.0.0")
    if is_local and scheme == "https":
        scheme = "http"
    if is_local and not port:
        port = 8090

    ws_scheme = "ws" if scheme == "http" else "wss"
    netloc = f"{hostname}:{port}" if port else hostname

    _AHS_HTTP_BASE = f"{scheme}://{netloc}/ahs"
    _AHS_WS_BASE = f"{ws_scheme}://{netloc}/ahs"


def get_http_base() -> str:
    return _AHS_HTTP_BASE


def get_ws_base() -> str:
    return _AHS_WS_BASE


def get_api_key() -> str:
    return _AHS_API_KEY


def _ws_url(session_id: str) -> str:
    return f"{_AHS_WS_BASE}/session/{session_id}/ws?api_key={_AHS_API_KEY}"


def _load_session_id() -> str | None:
    if SESSION_FILE.exists():
        return SESSION_FILE.read_text().strip() or None
    return None


def _save_session_id(sid: str) -> None:
    SESSION_FILE.write_text(sid)


def _load_user_id() -> str | None:
    if USER_ID_FILE.exists():
        uid = USER_ID_FILE.read_text().strip()
        return uid or None
    return None


# ---------------------------------------------------------------------------
# HTTP helpers (async via aiohttp)
# ---------------------------------------------------------------------------


async def _http_request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{_AHS_HTTP_BASE}{path}"
    headers = {"Content-Type": "application/json", "X-API-Key": _AHS_API_KEY}
    async with aiohttp.ClientSession() as session, session.request(method, url, json=body, headers=headers) as resp:
        if resp.status >= 400:
            # Server may return non-JSON (e.g. plain text from proxy/LB on 5xx)
            text = await resp.text()
            raise RuntimeError(f"HTTP {resp.status}: {text[:500]}")
        return dict(await resp.json(content_type=None))
