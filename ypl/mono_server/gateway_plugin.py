"""Gateway plugin protocol for the AHS monolith.

A GatewayPlugin wraps an external HTTP gateway (Slack, GitHub, etc.) into a
single, uniform interface that the monolith's ``server.py`` can manage
without knowing the implementation details of each gateway.

Protocol contract:
- ``name``        — URL-safe short name used in the ``/gw/<name>/`` prefix.
- ``env_flag``    — Name of the env-var / ``MonoConfig`` attribute that
                    enables/disables this plugin (e.g. ``GATEWAY_SLACK_ENABLED``).
- ``get_router()``  — Return the FastAPI ``APIRouter`` to mount at
                      ``/gw/<name>/``, or ``None`` if the gateway registers no
                      HTTP routes.
- ``startup()``     — Called once during server startup; returns opaque state
                      forwarded to ``shutdown()``.
- ``shutdown(state)`` — Called once during server shutdown; receives the value
                        returned by ``startup()``.

The protocol is intentionally structural (no base class) so plugins can be
implemented as lightweight classes or dataclasses without inheriting from a
heavyweight base.  New gateways — GitHub, webhook bridges, etc. — are wired
in by adding them to the ``_REGISTRY`` list inside ``server.discover_plugins``.
"""

from __future__ import annotations
from typing import Any, Protocol, runtime_checkable

from fastapi import APIRouter


@runtime_checkable
class GatewayPlugin(Protocol):
    """Structural protocol every gateway plugin must satisfy.

    Plugins are returned by ``server.discover_plugins()`` and managed in a
    uniform startup / shutdown loop.  The protocol is ``@runtime_checkable``
    so that ``isinstance(obj, GatewayPlugin)`` can be used in tests and guards
    without requiring an explicit base class.

    Attribute contract (readable on instances):
        name:     URL-safe short name — becomes the ``/gw/<name>/`` prefix.
        env_flag: Env-var name that toggles this plugin.  The monolith maps it
                  to the lower-cased ``MonoConfig`` field name (e.g.
                  ``GATEWAY_SLACK_ENABLED`` → ``gateway_slack_enabled``).
    """

    #: URL-safe short name — becomes the ``/gw/<name>/`` path prefix.
    name: str

    #: Environment variable that enables/disables this plugin.
    env_flag: str

    def get_router(self) -> APIRouter | None:
        """Return the FastAPI router for this gateway, or ``None``.

        When ``None`` is returned the plugin still participates in the
        lifespan (``startup`` / ``shutdown`` are called), but no routes are
        added to the parent application.
        """
        ...

    async def startup(self) -> Any:
        """Initialise the gateway subsystem.

        Called once during server startup, *before* the application begins
        accepting requests.

        Returns:
            Opaque state object forwarded verbatim to :meth:`shutdown`.  Use a
            dataclass for structured state, or return ``None`` if no state is
            required.
        """
        ...

    async def shutdown(self, state: Any) -> None:
        """Tear down the gateway subsystem.

        Called once during server shutdown, *after* the application has
        stopped accepting new requests.  Shutdown order is the reverse of
        startup order.

        Args:
            state: The value returned by :meth:`startup`.
        """
        ...
