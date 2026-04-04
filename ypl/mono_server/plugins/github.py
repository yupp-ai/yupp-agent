"""GitHub App webhook gateway plugin for the AHS monolith.

Wraps the existing GitHub webhook router in the
:class:`~ypl.mono_server.gateway_plugin.GatewayPlugin` protocol so that
``server.py`` can manage it through the uniform plugin interface.

Routes are mounted at ``/gw/github/`` in the monolith, producing:

    POST /gw/github/webhook/github   — GitHub App webhook receiver

Enable via the ``GATEWAY_GITHUB_ENABLED`` environment variable:

    GATEWAY_GITHUB_ENABLED=true

Default: **OFF** (``false``) — this gateway requires a valid GitHub App
webhook secret to be configured (``AHS_GITHUB_WEBHOOK_SECRET`` env var).
Without the secret every request is rejected with HTTP 503.

Webhook events handled
----------------------
- ``pull_request`` (``opened``, ``ready_for_review``, ``synchronize``)
- ``ping`` — acknowledged and silently dropped

All other event types are acknowledged and silently ignored.

Authentication
--------------
Every incoming request is verified against the HMAC-SHA256 signature
that GitHub sends in the ``X-Hub-Signature-256`` header.  The shared
secret must be configured via ``AHS_GITHUB_WEBHOOK_SECRET``.  No API key
is required — the webhook secret is the sole auth mechanism.

The standalone AHS server (``ypl/agent_harness_service/server.py``) is
unaffected — it continues to register its own webhook router directly.
Note: the monolith no longer exposes ``/ahs/webhook/github``; the canonical
webhook URL in the monolith is ``/gw/github/webhook/github``.
"""

from __future__ import annotations
from typing import Any

from fastapi import APIRouter

from ypl.agent_harness_service.github_webhook import webhook_router
from ypl.structured_logger import get_logger

logger = get_logger()


class GitHubGatewayPlugin:
    """GatewayPlugin implementation wrapping the GitHub webhook receiver.

    Routes are mounted at ``/gw/github/`` in the monolith.  Enable via
    ``GATEWAY_GITHUB_ENABLED=true``; default is **off**.

    The plugin receives GitHub App webhook events (``pull_request``, ``ping``)
    and dispatches them to the ``master-reviewer`` agent by creating AHS
    sessions.  Webhook signature verification (HMAC-SHA256 via
    ``X-Hub-Signature-256``) is handled inside the existing
    :func:`~ypl.agent_harness_service.github_webhook.github_webhook` endpoint.

    Example::

        plugin = GitHubGatewayPlugin()
        state = await plugin.startup()
        app.include_router(plugin.get_router(), prefix="/gw/github")
        ...
        await plugin.shutdown(state)
    """

    #: URL-safe name — becomes the ``/gw/<name>/`` path prefix.
    name: str = "github"

    #: ``MonoConfig`` field / env-var that enables this plugin.
    env_flag: str = "GATEWAY_GITHUB_ENABLED"

    def get_router(self) -> APIRouter:
        """Return the GitHub webhook APIRouter.

        The router carries the ``POST /webhook/github`` route (relative to the
        ``/gw/github`` mount point), resulting in the full canonical URL::

            POST /gw/github/webhook/github

        The same handler is also reachable at the backward-compat path
        ``POST /ahs/webhook/github`` when registered via ``_setup_ahs_router``.
        """
        return webhook_router

    async def startup(self) -> None:
        """No-op startup — the GitHub webhook receiver is stateless.

        The endpoint reads ``AHS_GITHUB_WEBHOOK_SECRET`` on every request, so
        there is no per-process state to initialise here.
        """
        logger.info("github_gateway_plugin_started")

    async def shutdown(self, state: Any) -> None:
        """No-op shutdown — nothing to tear down for a stateless webhook receiver.

        Args:
            state: Always ``None`` (the value returned by :meth:`startup`).
        """
        logger.info("github_gateway_plugin_stopped")
