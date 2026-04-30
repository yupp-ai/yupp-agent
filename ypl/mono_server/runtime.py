"""Monolith runtime detection.

Tiny, dependency-free module that records whether the current process is
running as the AHS+SAG+MCP monolith (``ypl.mono_server.server:app``) so that
in-process callers can bypass HTTP loopbacks that fail at the lifespan edges
(uvicorn closes the listener before ``__aexit__`` runs and only opens it
after ``__aenter__`` completes).

Kept in its own module — with **no** other ``ypl.*`` imports — so that any
package, including AHS internals (``ypl.agent_harness_service.service.*``),
can read the flag without introducing an import cycle.

Usage::

    # mono_server.server.combined_lifespan
    from ypl.mono_server.runtime import set_monolith_mode

    async with combined_lifespan(app):
        set_monolith_mode(True)
        try:
            ...
        finally:
            set_monolith_mode(False)

    # any in-process caller (e.g. courtesy broadcast)
    from ypl.mono_server.runtime import is_monolith_mode

    if is_monolith_mode():
        await add_reply(AddReplyRequest(...))
    else:
        await gateway.send_reply(...)
"""

from __future__ import annotations

_MONOLITH_MODE: bool = False


def set_monolith_mode(value: bool) -> None:
    """Set the monolith-mode flag.

    Called exactly once on entry into ``combined_lifespan`` (and reset on
    exit) so that downstream code can detect we are running in the same
    process as SAG and skip the HTTP loopback.
    """
    global _MONOLITH_MODE
    _MONOLITH_MODE = value


def is_monolith_mode() -> bool:
    """Return True if the current process is the AHS+SAG+MCP monolith.

    Returns False in the standalone AHS server, the standalone SAG server,
    the MCP-only server, and any test that has not explicitly opted in.
    """
    return _MONOLITH_MODE
