"""Tests for executors/mcp_config.py — MCP server resolution and workspace config writing.

After the phase-2 tool taxonomy refactor (PR for branch
``tw/mono-mcp-2-tool-taxonomy``) AHS no longer attaches an
``agcouch-mcp-server`` entry to a session's ``.mcp.json``: shared and
external-data tools register on the harness MCP via ``@shared_tool`` and
agents reach them through ``AHS_MCP_SECRET``. These tests pin that
contract so a future refactor cannot silently re-introduce the agcouch
detour.
"""

from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any

from ypl.agent_harness_service.common.types import SessionPermissions
from ypl.agent_harness_service.executors.mcp_config import (
    CODEX_HARNESS_BEARER_ENV,
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
        regression here would mean the AGCOUCH_MCP_TOKEN detour is back.
        """
        perms = SessionPermissions.full_access()
        ctx: dict[str, Any] = {"permissions": perms.model_dump(mode="json")}
        servers = resolve_mcp_servers(session_id="sess-1", session_context=ctx)
        assert list(servers.keys()) == ["harness"]
        assert "agcouch-mcp-server" not in servers

    def test_restricted_permissions_keep_only_harness(self) -> None:
        """Restricted sessions also see only the harness mount."""
        perms = SessionPermissions.restricted()  # allowed_servers=["harness"]
        ctx: dict[str, Any] = {"permissions": perms.model_dump(mode="json")}
        servers = resolve_mcp_servers(session_id="sess-1", session_context=ctx)
        assert "harness" in servers
        assert "agcouch-mcp-server" not in servers

    def test_slack_session_without_permissions_keeps_only_harness(self) -> None:
        """Slack sessions with no permissions still get the harness mount."""
        servers = resolve_mcp_servers(session_id="sess-1", is_slack=True)
        assert "harness" in servers
        assert "agcouch-mcp-server" not in servers

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

    def test_no_agcouch_authorization_header(self) -> None:
        """No agcouch entry — and therefore no AGCOUCH_MCP_TOKEN — anywhere in the dict."""
        servers = resolve_mcp_servers(session_id="sess-1")
        assert "agcouch-mcp-server" not in servers
        # Belt-and-suspenders: serialise to JSON and ensure the env-var name
        # never leaks through. A regression that re-introduces an agcouch
        # entry would expand ``${AGCOUCH_MCP_TOKEN}`` here.
        assert "AGCOUCH_MCP_TOKEN" not in json.dumps(servers)


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

    def test_no_agcouch_bearer_env_var(self) -> None:
        """No AGCOUCH_MCP_TOKEN is configured for any Codex MCP server.

        Pins the phase-2 invariant: Codex never sees the agcouch bearer
        token. A regression that re-introduces the agcouch mount would
        also re-add ``AGCOUCH_MCP_TOKEN`` here.
        """
        args = build_codex_mcp_args(session_id="sess-1")
        args_str = " ".join(args)
        assert "AGCOUCH_MCP_TOKEN" not in args_str
        assert "agcouch" not in args_str


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
