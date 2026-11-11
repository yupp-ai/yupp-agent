"""Tests for executors/mcp_config.py — MCP server resolution and workspace config writing.

After the phase-2 tool taxonomy refactor (PR for branch
``tw/mono-mcp-2-tool-taxonomy``) AHS no longer attaches an
``platform-mcp-server`` entry to a session's ``.mcp.json``: shared and
external-data tools register on the harness MCP via ``@shared_tool`` and
agents reach them through ``AHS_MCP_SECRET``. These tests pin that
contract so a future refactor cannot silently re-introduce the platform
detour.
"""

from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

from ypl.agent_harness_service.common.types import SessionPermissions
from ypl.agent_harness_service.executors.mcp_config import (
    CODEX_HARNESS_BEARER_ENV,
    browser_available,
    build_codex_mcp_args,
    build_codex_mcp_env,
    ensure_workspace_mcp_config,
    resolve_mcp_servers,
)


class TestResolveMcpServers:
    def test_harness_server_always_present(self) -> None:
        """Harness server is always injected regardless of permissions."""
        servers = resolve_mcp_servers(session_id="sess-1")
        assert "harness" in servers
        assert "url" in servers["harness"]
        assert "X-AHS-Session-ID" in servers["harness"]["headers"]
        assert servers["harness"]["headers"]["X-AHS-Session-ID"] == "sess-1"
        # Security: auth token header must be present
        assert "X-AHS-Token" in servers["harness"]["headers"]

    def test_session_id_in_harness_headers(self) -> None:
        servers = resolve_mcp_servers(session_id="my-session-123")
        assert servers["harness"]["headers"]["X-AHS-Session-ID"] == "my-session-123"

    def test_only_harness_in_full_access(self) -> None:
        """Full-access sessions still get only the harness mount.

        Pins the phase-2 invariant: AHS reaches every tool through harness
        MCP, so the agent's ``.mcp.json`` advertises only ``harness``. A
        regression here would mean the PLATFORM_MCP_TOKEN detour is back.
        """
        perms = SessionPermissions.full_access()
        ctx: dict[str, Any] = {"permissions": perms.model_dump(mode="json")}
        servers = resolve_mcp_servers(session_id="sess-1", session_context=ctx)
        assert list(servers.keys()) == ["harness"]
        assert "platform-mcp-server" not in servers

    def test_restricted_permissions_keep_only_harness(self) -> None:
        """Restricted sessions also see only the harness mount."""
        perms = SessionPermissions.restricted()  # allowed_servers=["harness"]
        ctx: dict[str, Any] = {"permissions": perms.model_dump(mode="json")}
        servers = resolve_mcp_servers(session_id="sess-1", session_context=ctx)
        assert "harness" in servers
        assert "platform-mcp-server" not in servers

    def test_slack_session_without_permissions_keeps_only_harness(self) -> None:
        """Slack sessions with no permissions still get the harness mount."""
        servers = resolve_mcp_servers(session_id="sess-1", is_slack=True)
        assert "harness" in servers
        assert "platform-mcp-server" not in servers

    def test_harness_gets_user_id_header(self) -> None:
        """User ID from session context is injected into harness headers.

        The harness auth middleware validates ``AHS_MCP_SECRET`` then
        trusts the AHS-stamped ``X-User-ID`` for tool attribution. AHS
        writes the ``.mcp.json`` in a sandboxed location the agent
        cannot modify.
        """
        ctx: dict[str, Any] = {"user_id": "user-abc-123"}
        servers = resolve_mcp_servers(session_id="sess-1", session_context=ctx)
        assert servers["harness"]["headers"]["X-User-ID"] == "user-abc-123"

    def test_current_turn_user_id_takes_priority(self) -> None:
        """current_turn_user_id overrides user_id for the harness header."""
        ctx: dict[str, Any] = {"user_id": "static-user", "current_turn_user_id": "turn-user"}
        servers = resolve_mcp_servers(session_id="sess-1", session_context=ctx)
        assert servers["harness"]["headers"]["X-User-ID"] == "turn-user"

    def test_agent_name_header_injected(self) -> None:
        """Agent name is injected into harness headers when provided."""
        servers = resolve_mcp_servers(session_id="sess-1", agent_name="sre")
        assert servers["harness"]["headers"]["X-AHS-Agent-Name"] == "sre"

    def test_no_platform_authorization_header(self) -> None:
        """No platform entry — and therefore no PLATFORM_MCP_TOKEN — anywhere in the dict."""
        servers = resolve_mcp_servers(session_id="sess-1")
        assert "platform-mcp-server" not in servers
        # Belt-and-suspenders: serialise to JSON and ensure the env-var name
        # never leaks through. A regression that re-introduces an platform
        # entry would expand ``${PLATFORM_MCP_TOKEN}`` here.
        assert "PLATFORM_MCP_TOKEN" not in json.dumps(servers)


class TestEnsureWorkspaceMcpConfig:
    def test_writes_mcp_json_to_workspace(self, tmp_path: Path) -> None:
        """ensure_workspace_mcp_config writes a valid .mcp.json to the workspace."""
        workspace = str(tmp_path)
        ensure_workspace_mcp_config(workspace, session_id="sess-abc")

        mcp_path = os.path.join(workspace, ".mcp.json")
        assert os.path.isfile(mcp_path)

        with open(mcp_path) as f:
            config = json.load(f)

        assert "mcpServers" in config
        assert "harness" in config["mcpServers"]

    def test_mcp_json_contains_session_id(self, tmp_path: Path) -> None:
        workspace = str(tmp_path)
        ensure_workspace_mcp_config(workspace, session_id="my-session")

        with open(os.path.join(workspace, ".mcp.json")) as f:
            config = json.load(f)

        harness_headers = config["mcpServers"]["harness"]["headers"]
        assert harness_headers["X-AHS-Session-ID"] == "my-session"

    def test_empty_workspace_returns_early(self) -> None:
        """ensure_workspace_mcp_config returns early without writing when workspace is empty."""
        # Should not raise even without a tmp dir
        ensure_workspace_mcp_config("", session_id="sess-1")
        # No assertions needed — just verify no exception

    def test_atomic_write_replaces_existing(self, tmp_path: Path) -> None:
        """A second call overwrites the existing .mcp.json atomically."""
        workspace = str(tmp_path)
        ensure_workspace_mcp_config(workspace, session_id="first-session")
        ensure_workspace_mcp_config(workspace, session_id="second-session")

        with open(os.path.join(workspace, ".mcp.json")) as f:
            config = json.load(f)

        assert config["mcpServers"]["harness"]["headers"]["X-AHS-Session-ID"] == "second-session"


class TestBuildCodexMcpArgs:
    def test_returns_list_of_flags(self) -> None:
        args = build_codex_mcp_args(session_id="sess-1")
        assert isinstance(args, list)

    def test_harness_server_args_present(self) -> None:
        args = build_codex_mcp_args(session_id="sess-1")
        # Args should be in -c 'key=value' pairs
        assert "-c" in args
        # Harness URL and bearer env var should be set
        args_str = " ".join(args)
        assert "harness" in args_str
        assert CODEX_HARNESS_BEARER_ENV in args_str

    def test_no_platform_bearer_env_var(self) -> None:
        """No PLATFORM_MCP_TOKEN is configured for any Codex MCP server.

        Pins the phase-2 invariant: Codex never sees the platform bearer
        token. A regression that re-introduces the platform mount would
        also re-add ``PLATFORM_MCP_TOKEN`` here.
        """
        args = build_codex_mcp_args(session_id="sess-1")
        args_str = " ".join(args)
        assert "PLATFORM_MCP_TOKEN" not in args_str
        assert "platform" not in args_str

    def test_passes_agent_and_external_mcp_context_to_resolver(self) -> None:
        with patch(
            "ypl.agent_harness_service.executors.mcp_config.resolve_mcp_servers",
            return_value={"harness": {"url": "http://127.0.0.1:8090/mcp/harness/"}},
        ) as mock_resolve:
            build_codex_mcp_args(
                session_id="sess-1",
                session_context={"user_id": "user-1"},
                is_slack=True,
                agent_name="veronia",
                external_mcps=["gmail"],
            )

        mock_resolve.assert_called_once_with(
            "sess-1",
            {"user_id": "user-1"},
            True,
            agent_name="veronia",
            external_mcps=["gmail"],
            workspace=None,
        )


class TestBuildCodexMcpEnv:
    def test_returns_bearer_env_var(self) -> None:
        env = build_codex_mcp_env("sess-abc")
        assert CODEX_HARNESS_BEARER_ENV in env

    def test_bearer_token_format(self) -> None:
        """Bearer token is formatted as <secret>:<session_id>."""
        env = build_codex_mcp_env("my-session-id")
        token = env[CODEX_HARNESS_BEARER_ENV]
        assert ":" in token
        assert token.endswith(":my-session-id")

    def test_different_sessions_different_tokens(self) -> None:
        env1 = build_codex_mcp_env("session-1")
        env2 = build_codex_mcp_env("session-2")
        assert env1[CODEX_HARNESS_BEARER_ENV] != env2[CODEX_HARNESS_BEARER_ENV]


_MCP_CONFIG = "ypl.agent_harness_service.executors.mcp_config"


def _full_ctx() -> dict[str, Any]:
    return {"permissions": SessionPermissions.full_access().model_dump(mode="json")}


class TestBrowserMcpServer:
    """Opt-in Playwright browser (AHS_BROWSER_ENABLED)."""

    def test_absent_when_disabled(self) -> None:
        with patch(f"{_MCP_CONFIG}.AHS_BROWSER_ENABLED", False):
            servers = resolve_mcp_servers(session_id="s", session_context=_full_ctx(), workspace="/ws")
        assert "playwright" not in servers
        assert browser_available(_full_ctx()) is False

    def test_stdio_entry_when_enabled(self) -> None:
        with (
            patch(f"{_MCP_CONFIG}.AHS_BROWSER_ENABLED", True),
            patch(f"{_MCP_CONFIG}.AHS_BROWSER_CHROMIUM_SANDBOX", False),
            patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "/opt/ms-playwright"}),
        ):
            servers = resolve_mcp_servers(session_id="s", session_context=_full_ctx(), workspace="/ws")
        entry = servers["playwright"]
        assert entry["type"] == "stdio"
        assert entry["command"] == "playwright-mcp"
        args = entry["args"]
        assert {"--headless", "--isolated", "--no-sandbox"} <= set(args)
        assert args[args.index("--browser") + 1] == "chromium"
        assert args[args.index("--output-dir") + 1] == "/ws/browser"
        assert entry["env"] == {"PLAYWRIGHT_BROWSERS_PATH": "/opt/ms-playwright"}

    def test_chromium_sandbox_flag(self) -> None:
        with (
            patch(f"{_MCP_CONFIG}.AHS_BROWSER_ENABLED", True),
            patch(f"{_MCP_CONFIG}.AHS_BROWSER_CHROMIUM_SANDBOX", True),
        ):
            servers = resolve_mcp_servers(session_id="s", session_context=_full_ctx())
        assert "--no-sandbox" not in servers["playwright"]["args"]
        assert "--output-dir" not in servers["playwright"]["args"]

    def test_restricted_session_gets_no_browser(self) -> None:
        ctx: dict[str, Any] = {"permissions": SessionPermissions.restricted().model_dump(mode="json")}
        with patch(f"{_MCP_CONFIG}.AHS_BROWSER_ENABLED", True):
            servers = resolve_mcp_servers(session_id="s", session_context=ctx)
            assert browser_available(ctx) is False
            assert browser_available(None, is_slack=True) is False
            assert browser_available(_full_ctx()) is True
        assert "playwright" not in servers

    def test_written_to_workspace_mcp_json(self, tmp_path: Path) -> None:
        with patch(f"{_MCP_CONFIG}.AHS_BROWSER_ENABLED", True):
            ensure_workspace_mcp_config(str(tmp_path), session_id="s", session_context=_full_ctx())
        data = json.loads((tmp_path / ".mcp.json").read_text())
        assert data["mcpServers"]["playwright"]["type"] == "stdio"
        assert str(tmp_path / "browser") in data["mcpServers"]["playwright"]["args"]

    def test_codex_stdio_args(self) -> None:
        with (
            patch(f"{_MCP_CONFIG}.AHS_BROWSER_ENABLED", True),
            patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "/opt/ms-playwright"}),
        ):
            args = build_codex_mcp_args(session_id="s", session_context=_full_ctx(), workspace="/ws")
        flags = [args[i + 1] for i, a in enumerate(args) if a == "-c"]
        assert 'mcp_servers.playwright.command="playwright-mcp"' in flags
        args_flag = next(f for f in flags if f.startswith("mcp_servers.playwright.args="))
        assert json.loads(args_flag.split("=", 1)[1])[-2:] == ["--output-dir", "/ws/browser"]
        assert 'mcp_servers.playwright.env={ PLAYWRIGHT_BROWSERS_PATH = "/opt/ms-playwright" }' in flags
        # No url-style flags leak in for the stdio server.
        assert not any(f.startswith("mcp_servers.playwright.url") for f in flags)
