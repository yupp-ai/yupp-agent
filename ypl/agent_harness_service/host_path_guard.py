"""Host-based path allowlist middleware.

When the mono-server is exposed on multiple subdomains (e.g. via Cloudflare
Tunnel), we want some subdomains to only reach specific path prefixes --
for example, ``mcp.example.com`` should only reach ``/mcp/*`` and ``/health``,
not ``/ahs/*`` or ``/gw/slack/*``.

This middleware inspects the incoming ``Host`` header and, if the host (with
port stripped) matches a configured scoped host, enforces its path allowlist.
Requests whose path does not start with any allowed prefix return HTTP 404.
Hosts not present in the map are unaffected -- direct LAN / localhost access
continues to reach every route.
"""

from __future__ import annotations
from collections.abc import Awaitable, Callable
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

_DEFAULT_404_BODY: Final[bytes] = b'{"detail":"Not Found"}'


def _path_matches(path: str, prefix: str) -> bool:
    """True iff *path* is exactly *prefix* or starts with ``prefix + '/'``.

    Prevents accidental substring matches (``/mcp`` must not match ``/mcpother``).
    """
    return path == prefix or path.startswith(prefix + "/")


class HostPathGuardMiddleware(BaseHTTPMiddleware):
    """Restrict scoped subdomains to a configured path allowlist.

    Parameters
    ----------
    host_allowlist:
        Map of ``{hostname: [allowed_path_prefix, ...]}``. A request whose
        ``Host`` header (port stripped, lowercased) matches a key may only
        access paths that start with one of the listed prefixes; all others
        get a 404. Hosts not present in the map pass through untouched.

    An empty list for a host blocks every path on that host -- useful for
    quickly "closing" a subdomain without removing its Cloudflare ingress.
    """

    def __init__(self, app: ASGIApp, host_allowlist: dict[str, list[str]]) -> None:
        super().__init__(app)
        self._host_allowlist = host_allowlist

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        raw_host = request.headers.get("host", "")
        host = raw_host.split(":", 1)[0].lower()

        allowed_prefixes = self._host_allowlist.get(host)
        if allowed_prefixes is None:
            # Host not scoped -- pass through.
            return await call_next(request)

        path = request.url.path
        if any(_path_matches(path, prefix) for prefix in allowed_prefixes):
            return await call_next(request)

        return Response(
            content=_DEFAULT_404_BODY,
            status_code=404,
            media_type="application/json",
        )
