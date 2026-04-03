"""Slack Agent Gateway plugin for the AHS monolith.

Wraps the existing SAG lifespan helpers (``sag_startup`` / ``sag_shutdown``)
and the SAG ``APIRouter`` in the :class:`~ypl.mono_server.gateway_plugin.GatewayPlugin`
protocol so that ``server.py`` can manage them through the uniform plugin interface.

The standalone SAG server (``ypl/slack_agent_gateway/server.py``) is
unaffected — it continues to call ``sag_startup`` / ``sag_shutdown`` directly
inside its own FastAPI lifespan.  Both modes can coexist in the same codebase.
"""

from __future__ import annotations
from typing import Any

from fastapi import APIRouter

from ypl.slack_agent_gateway.lifespan import SAGState, sag_shutdown, sag_startup
from ypl.slack_agent_gateway.routes import router as sag_router


class SlackGatewayPlugin:
    """GatewayPlugin implementation that wraps the Slack Agent Gateway.

    Routes are mounted at ``/gw/slack/`` in the monolith.  Enable/disable via
    the ``GATEWAY_SLACK_ENABLED`` environment variable (default: ``true``).

    Example::

        plugin = SlackGatewayPlugin()
        state = await plugin.startup()
        app.include_router(plugin.get_router(), prefix="/gw/slack")
        ...
        await plugin.shutdown(state)
    """

    #: URL-safe name — used as the ``/gw/<name>/`` prefix.
    name: str = "slack"

    #: ``MonoConfig`` field / env-var that enables this plugin.
    env_flag: str = "GATEWAY_SLACK_ENABLED"

    def get_router(self) -> APIRouter:
        """Return the SAG APIRouter.

        The router carries all SAG routes (``/slack/events``, ``/sessions/*``,
        ``/messages/*``, ``/bot-father/*``, etc.) without a prefix — the
        monolith mounts it at ``/gw/slack``.
        """
        return sag_router

    async def startup(self) -> SAGState:
        """Initialise the SAG subsystem.

        Configures asyncio-compatible structured logging and launches the
        background flush-manager task.

        Returns:
            :class:`~ypl.slack_agent_gateway.lifespan.SAGState` holding the
            flush-manager task handle; must be forwarded to :meth:`shutdown`.
        """
        return await sag_startup()

    async def shutdown(self, state: Any) -> None:
        """Stop the SAG flush-manager.

        Cancels and awaits the flush-manager task that was started by
        :meth:`startup`.

        Args:
            state: :class:`~ypl.slack_agent_gateway.lifespan.SAGState`
                returned by :meth:`startup`.
        """
        await sag_shutdown(state)
