"""Tests for the GitHub gateway plugin (GitHubGatewayPlugin).

Covers:
  1. Protocol compliance — GitHubGatewayPlugin satisfies GatewayPlugin.
  2. Router exposure — get_router() returns an APIRouter with the webhook route.
  3. Startup / shutdown — lifecycle methods are no-ops that log and return None.
  4. Signature verification — _verify_signature helper accepts/rejects correctly.
  5. Config default — gateway_github_enabled is False by default.
  6. Discover-plugins integration — plugin is discovered iff env flag is set.
  7. Webhook endpoint — ping event acknowledged, unknown events skipped,
     signature rejection returns 401, missing secret returns 503.
"""

from __future__ import annotations
import hashlib
import hmac
import json
import os
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from ypl.agent_harness_service.github_webhook import _verify_signature
from ypl.mono_server.gateway_plugin import GatewayPlugin
from ypl.mono_server.plugins.github import GitHubGatewayPlugin

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hmac_sig(secret: str, body: bytes) -> str:
    """Compute the expected HMAC-SHA256 signature in GitHub's format."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ---------------------------------------------------------------------------
# 1. Protocol compliance
# ---------------------------------------------------------------------------


class TestGitHubGatewayPluginProtocol:
    """GitHubGatewayPlugin must satisfy the GatewayPlugin structural protocol."""

    def test_satisfies_gateway_plugin_protocol(self) -> None:
        plugin = GitHubGatewayPlugin()
        assert isinstance(plugin, GatewayPlugin), "GitHubGatewayPlugin does not satisfy the GatewayPlugin protocol"

    def test_name_attribute(self) -> None:
        assert GitHubGatewayPlugin.name == "github"

    def test_env_flag_attribute(self) -> None:
        assert GitHubGatewayPlugin.env_flag == "GATEWAY_GITHUB_ENABLED"


# ---------------------------------------------------------------------------
# 2. Router exposure
# ---------------------------------------------------------------------------


class TestGitHubGatewayPluginRouter:
    """get_router() returns a usable APIRouter with the webhook endpoint."""

    def test_get_router_returns_api_router(self) -> None:
        plugin = GitHubGatewayPlugin()
        router = plugin.get_router()
        assert isinstance(router, APIRouter)

    def test_router_has_webhook_route(self) -> None:
        plugin = GitHubGatewayPlugin()
        router = plugin.get_router()
        # webhook_router has prefix="/webhook" and endpoint "/github", so FastAPI
        # stores the combined path as "/webhook/github".
        route_paths = {getattr(r, "path", "") for r in router.routes}
        assert "/webhook/github" in route_paths, f"Expected /webhook/github route, got: {route_paths}"

    def test_get_router_returns_same_instance_on_repeat_calls(self) -> None:
        """get_router() must return the same router object each time (idempotent)."""
        plugin = GitHubGatewayPlugin()
        assert plugin.get_router() is plugin.get_router()


# ---------------------------------------------------------------------------
# 3. Startup / shutdown
# ---------------------------------------------------------------------------


class TestGitHubGatewayPluginLifecycle:
    """startup() and shutdown() are no-ops that return None."""

    @pytest.mark.asyncio
    async def test_startup_returns_none(self) -> None:
        plugin = GitHubGatewayPlugin()
        # startup() is typed -> None; just verify it doesn't raise
        await plugin.startup()

    @pytest.mark.asyncio
    async def test_shutdown_accepts_none_state(self) -> None:
        plugin = GitHubGatewayPlugin()
        # Should not raise
        await plugin.shutdown(None)

    @pytest.mark.asyncio
    async def test_startup_then_shutdown_roundtrip(self) -> None:
        plugin = GitHubGatewayPlugin()
        await plugin.startup()
        await plugin.shutdown(None)  # Must not raise


# ---------------------------------------------------------------------------
# 4. Signature verification
# ---------------------------------------------------------------------------


class TestVerifySignature:
    """_verify_signature correctly validates HMAC-SHA256 webhook signatures."""

    _SECRET = "test-webhook-secret"
    _BODY = b'{"action": "opened"}'

    def test_valid_signature_accepted(self) -> None:
        sig = _hmac_sig(self._SECRET, self._BODY)
        assert _verify_signature(self._BODY, sig, self._SECRET) is True

    def test_wrong_secret_rejected(self) -> None:
        sig = _hmac_sig("wrong-secret", self._BODY)
        assert _verify_signature(self._BODY, sig, self._SECRET) is False

    def test_tampered_body_rejected(self) -> None:
        sig = _hmac_sig(self._SECRET, self._BODY)
        tampered = self._BODY + b" extra"
        assert _verify_signature(tampered, sig, self._SECRET) is False

    def test_missing_signature_rejected(self) -> None:
        assert _verify_signature(self._BODY, None, self._SECRET) is False

    def test_malformed_signature_no_prefix_rejected(self) -> None:
        # Valid hex digest but without the "sha256=" prefix
        digest = hmac.new(self._SECRET.encode(), self._BODY, hashlib.sha256).hexdigest()
        assert _verify_signature(self._BODY, digest, self._SECRET) is False

    def test_empty_body_valid_signature_accepted(self) -> None:
        sig = _hmac_sig(self._SECRET, b"")
        assert _verify_signature(b"", sig, self._SECRET) is True


# ---------------------------------------------------------------------------
# 5. Config default
# ---------------------------------------------------------------------------


class TestMonoConfigGitHubDefault:
    """gateway_github_enabled defaults to False (explicit opt-in required)."""

    def test_github_disabled_by_default(self) -> None:
        from ypl.mono_server.config import MonoConfig

        cfg = MonoConfig()
        assert cfg.gateway_github_enabled is False, (
            "GitHub gateway must be off by default — set GATEWAY_GITHUB_ENABLED=true to enable"
        )

    def test_github_enabled_via_env(self) -> None:
        with patch.dict(os.environ, {"GATEWAY_GITHUB_ENABLED": "true"}):
            from ypl.mono_server.config import MonoConfig

            cfg = MonoConfig()
            assert cfg.gateway_github_enabled is True

    def test_github_enabled_via_env_numeric(self) -> None:
        with patch.dict(os.environ, {"GATEWAY_GITHUB_ENABLED": "1"}):
            from ypl.mono_server.config import MonoConfig

            cfg = MonoConfig()
            assert cfg.gateway_github_enabled is True

    def test_github_disabled_via_env(self) -> None:
        with patch.dict(os.environ, {"GATEWAY_GITHUB_ENABLED": "false"}):
            from ypl.mono_server.config import MonoConfig

            cfg = MonoConfig()
            assert cfg.gateway_github_enabled is False


# ---------------------------------------------------------------------------
# 6. discover_plugins integration
# ---------------------------------------------------------------------------


class TestDiscoverPluginsGitHub:
    """GitHubGatewayPlugin is included / excluded based on the env flag."""

    def test_github_plugin_excluded_by_default(self) -> None:
        from ypl.mono_server.config import MonoConfig
        from ypl.mono_server.server import discover_plugins

        cfg = MonoConfig()
        plugins = discover_plugins(cfg)
        names = [p.name for p in plugins]
        assert "github" not in names, f"GitHub plugin should be disabled by default; got: {names}"

    def test_github_plugin_included_when_enabled(self) -> None:
        from ypl.mono_server.config import MonoConfig
        from ypl.mono_server.server import discover_plugins

        with patch.dict(os.environ, {"GATEWAY_GITHUB_ENABLED": "true"}):
            cfg = MonoConfig()
        plugins = discover_plugins(cfg)
        names = [p.name for p in plugins]
        assert "github" in names, f"Expected github in plugins; got: {names}"

    def test_slack_plugin_still_included_when_github_disabled(self) -> None:
        """Disabling GitHub must not affect the Slack plugin."""
        from ypl.mono_server.config import MonoConfig
        from ypl.mono_server.server import discover_plugins

        cfg = MonoConfig()  # github disabled by default
        plugins = discover_plugins(cfg)
        names = [p.name for p in plugins]
        assert "slack" in names, f"Slack plugin should still be enabled; got: {names}"


# ---------------------------------------------------------------------------
# 7. Webhook endpoint behaviour (hermetic, no DB / Redis)
# ---------------------------------------------------------------------------


def _make_github_client(secret: str = "test-secret") -> TestClient:
    """Return a TestClient for a minimal app that mounts the github webhook router."""
    from fastapi import FastAPI
    from fastapi.responses import ORJSONResponse
    from ypl.agent_harness_service.github_webhook import webhook_router

    mini_app = FastAPI(default_response_class=ORJSONResponse)
    mini_app.include_router(webhook_router, prefix="/gw/github")

    return TestClient(mini_app, raise_server_exceptions=False)


class TestWebhookEndpointHermetic:
    """Webhook endpoint rejects bad sigs, handles ping, skips unknown events."""

    _SECRET = "test-webhook-secret"

    def _post(
        self,
        client: TestClient,
        body: dict[str, Any],
        *,
        event: str = "pull_request",
        sig_override: str | None = None,
    ) -> Any:
        """POST a signed webhook payload.

        Args:
            client:       TestClient for the mini app.
            body:         JSON-serialisable payload dict.
            event:        Value for the ``X-GitHub-Event`` header.
            sig_override: If provided, use this raw value for the
                          ``X-Hub-Signature-256`` header instead of computing
                          a real HMAC.  Use to test invalid-signature paths.
        """
        raw = json.dumps(body).encode()
        sig = sig_override if sig_override is not None else _hmac_sig(self._SECRET, raw)
        return client.post(
            "/gw/github/webhook/github",
            content=raw,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": event,
                "Content-Type": "application/json",
            },
        )

    def test_missing_secret_returns_503(self) -> None:
        """When AHS_GITHUB_WEBHOOK_SECRET is empty, the endpoint returns 503.

        The secret check fires before signature verification, so any request
        (even a validly-signed one) returns 503 when the secret is unset.
        """
        from ypl.backend.config import settings

        client = _make_github_client(self._SECRET)
        with patch.object(settings, "AHS_GITHUB_WEBHOOK_SECRET", ""):
            r = self._post(client, {}, event="ping")
        assert r.status_code == 503

    def test_invalid_signature_returns_401(self) -> None:
        """Wrong HMAC digest → 401 Unauthorized."""
        from ypl.backend.config import settings

        client = _make_github_client(self._SECRET)
        with patch.object(settings, "AHS_GITHUB_WEBHOOK_SECRET", self._SECRET):
            r = self._post(client, {"foo": "bar"}, sig_override="sha256=badhex")
        assert r.status_code == 401

    def test_ping_event_acknowledged(self) -> None:
        """Valid ping event → 200 with ok:true."""
        from ypl.backend.config import settings

        client = _make_github_client(self._SECRET)
        with patch.object(settings, "AHS_GITHUB_WEBHOOK_SECRET", self._SECRET):
            r = self._post(client, {"zen": "Keep it logically awesome."}, event="ping")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["event"] == "ping"

    def test_unknown_event_skipped(self) -> None:
        """Unhandled event type (e.g. 'push') → 200 with skipped:true."""
        from ypl.backend.config import settings

        client = _make_github_client(self._SECRET)
        with patch.object(settings, "AHS_GITHUB_WEBHOOK_SECRET", self._SECRET):
            r = self._post(client, {}, event="push")
        assert r.status_code == 200
        data = r.json()
        assert data["skipped"] is True
        assert data["event"] == "push"

    def test_unhandled_pr_action_skipped(self) -> None:
        """pull_request with action='closed' → 200 with skipped:true."""
        from ypl.backend.config import settings

        client = _make_github_client(self._SECRET)
        with patch.object(settings, "AHS_GITHUB_WEBHOOK_SECRET", self._SECRET):
            r = self._post(
                client,
                {"action": "closed", "pull_request": {"draft": False}},
            )
        assert r.status_code == 200
        data = r.json()
        assert data["skipped"] is True
