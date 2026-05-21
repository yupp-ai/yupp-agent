"""Renderer registry — lazy factory + protocol.

Renderer modules call :func:`register` at import time with a factory that
constructs the renderer instance on first use. :func:`get` instantiates the
factory the first time it's asked for a given name and caches the result.
This keeps cold-start cheap: ``matplotlib`` is only imported the first time
a formula fence appears in a reply.

Tests can install fakes by calling :func:`register` directly (or
:func:`_reset_for_tests` between cases).
"""

from __future__ import annotations
from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class Renderer(Protocol):
    """A pluggable content renderer.

    Implementations are sync — the integration layer wraps calls in
    ``asyncio.to_thread`` so we don't block the event loop on subprocess
    or HTTP work.
    """

    name: str
    output_filename: str

    def render(self, source: str) -> bytes:
        """Render ``source`` to PNG bytes. Raise ``RenderError`` on failure."""
        ...


_FACTORIES: dict[str, Callable[[], Renderer]] = {}
_INSTANCES: dict[str, Renderer] = {}


def register(name: str, factory: Callable[[], Renderer]) -> None:
    """Register a renderer factory under ``name``.

    Re-registering the same name overwrites the previous factory and clears
    any cached instance — useful for tests installing fakes.
    """
    _FACTORIES[name] = factory
    _INSTANCES.pop(name, None)


def get(name: str) -> Renderer | None:
    """Return the renderer for ``name`` or ``None`` if not registered."""
    cached = _INSTANCES.get(name)
    if cached is not None:
        return cached
    factory = _FACTORIES.get(name)
    if factory is None:
        return None
    instance = factory()
    _INSTANCES[name] = instance
    return instance


def is_registered(name: str) -> bool:
    """Return True if a renderer is registered (without instantiating it)."""
    return name in _FACTORIES


def registered_names() -> list[str]:
    """List all registered renderer names (canonical, not including aliases)."""
    return sorted(_FACTORIES.keys())


def _reset_for_tests() -> None:
    """Clear all registrations. Use only from test setup."""
    _FACTORIES.clear()
    _INSTANCES.clear()
