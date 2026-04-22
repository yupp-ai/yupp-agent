"""Tests for executors/mcp_config.py — MCP server resolution and workspace config writing."""

from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

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

    def test_full_access_keeps_all_servers(self) -> None:
        """Full access permissions keeps both agcouch and harness servers."""
        perms = SessionPermissions.full_access()
        ctx: dict[str, Any] = {"permissions": perms.model_dump(mode="json")}
        servers = resolve_mcp_servers(session_id="sess-1", session_context=ctx)
        assert "harness" in servers
        assert "agcouch-mcp-server" in servers

    def test_restricted_permissions_removes_agcouch(self) -> None:
        """Restricted permissions removes servers not in allowed_servers list."""
        perms = SessionPermissions.restricted()  # allowed_servers=["harness"]
        ctx: dict[str, Any] = {"permissions": perms.model_dump(mode="json")}
        servers = resolve_mcp_servers(session_id="sess-1", session_context=ctx)
        assert "harness" in servers
        assert "agcouch-mcp-server" not in servers

    def test_slack_session_without_permissions_is_restricted(self) -> None:
        """Slack session with no permissions defaults to restricted (fail-secure)."""
        servers = resolve_mcp_servers(session_id="sess-1", is_slack=True)
        # Only harness should be present (restricted removes agcouch)
        assert "harness" in servers
        assert "agcouch-mcp-server" not in servers

    def test_agcouch_gets_user_id_header(self) -> None:
        """User ID from session context is injected into agcouch headers."""
        ctx: dict[str, Any] = {"user_id": "user-abc-123"}
        servers = resolve_mcp_servers(session_id="sess-1", session_context=ctx)
        assert servers["agcouch-mcp-server"]["headers"]["X-User-ID"] == "user-abc-123"

    def test_current_turn_user_id_takes_priority(self) -> None:
        """current_turn_user_id overrides user_id for agcouch header."""
        ctx: dict[str, Any] = {"user_id": "static-user", "current_turn_user_id": "turn-user"}
        servers = resolve_mcp_servers(session_id="sess-1", session_context=ctx)
        assert servers["agcouch-mcp-server"]["headers"]["X-User-ID"] == "turn-user"

    def test_agent_name_header_injected(self) -> None:
        """Agent name is injected into agcouch headers when provided."""
        servers = resolve_mcp_servers(session_id="sess-1", agent_name="sre")
        assert servers["agcouch-mcp-server"]["headers"]["X-AHS-Agent-Name"] == "sre"

    def test_agcouch_has_authorization_header(self) -> None:
        """Agcouch server is built with a Bearer Authorization header from settings."""
        servers = resolve_mcp_servers(session_id="sess-1")
        auth = servers["agcouch-mcp-server"]["headers"].get("Authorization", "")
        assert auth.startswith("Bearer ")

    def test_no_agcouch_when_settings_empty(self) -> None:
        """If AGCOUCH_MCP_SERVER_URL is empty the server is not built."""
        with patch("ypl.agent_harness_service.executors.mcp_config.settings") as mock_settings:
            mock_settings.AGCOUCH_MCP_SERVER_NAME = "agcouch-mcp-server"
            mock_settings.AGCOUCH_MCP_SERVER_URL = ""
            mock_settings.AGCOUCH_MCP_TOKEN = ""
            servers = resolve_mcp_servers(session_id="sess-1")
        assert "agcouch-mcp-server" not in servers
        assert "harness" in servers  # harness still injected


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

    def test_agcouch_bearer_env_var(self) -> None:
        """Agcouch MCP server gets AGCOUCH_MCP_TOKEN as bearer env var."""
        args = build_codex_mcp_args(session_id="sess-1")
        args_str = " ".join(args)
        assert "AGCOUCH_MCP_TOKEN" in args_str


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
