"""Tests for build_subprocess_env — subprocess environment sandboxing."""

from unittest.mock import patch

from ypl.agent_harness_service.executors.runner import build_subprocess_env


class TestBuildSubprocessEnv:
    """Verify that the subprocess env allowlist/blocklist works correctly."""

    def _build_with_env(self, env: dict[str, str]) -> dict[str, str]:
        """Helper: call build_subprocess_env with a controlled os.environ.

        Patches ``os.path.isdir`` to False so the optional venv-bin prepend in
        ``build_subprocess_env`` (which triggers when /opt/yupp-agent/.venv/bin
        exists on the test host) doesn't pollute PATH assertions. The venv-bin
        prepend is covered separately in test_runner_extended.
        """
        with (
            patch("ypl.agent_harness_service.executors.runner.os.environ", env),
            patch("ypl.agent_harness_service.executors.runner.os.path.isdir", return_value=False),
        ):
            return build_subprocess_env()

    # -- Allowed exact vars pass through -----------------------------------

    def test_allowed_exact_vars_pass_through(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "HOME": "/home/agent",
            "ANTHROPIC_API_KEY": "sk-ant-xxx",
            "OPENAI_API_KEY": "sk-xxx",
            "ENVIRONMENT": "production",
            "GITHUB_TOKEN": "ghp_abc",
            "SSH_AUTH_SOCK": "/tmp/ssh.sock",
            "AHS_DATA_DIR": "/data",
            "AHS_REPOS_DIR": "/data/ahs/repos",
        }
        result = self._build_with_env(env)
        # PATH gets ~/.local/bin prepended by build_subprocess_env
        assert result["PATH"] == "/home/agent/.local/bin:/usr/bin", "PATH should have ~/.local/bin prepended"
        for key, value in env.items():
            if key == "PATH":
                continue  # checked above with prepended local_bin
            assert result[key] == value, f"{key} should pass through"

    def test_platform_mcp_token_not_forwarded(self) -> None:
        """AHS no longer forwards PLATFORM_MCP_TOKEN to subprocesses.

        Phase 2 of the mono+MCP unification removed the platform detour from
        the AHS executor: agents reach every shared / external-data tool
        via the harness MCP using ``AHS_MCP_SECRET``. A regression that
        re-adds ``PLATFORM_MCP_TOKEN`` to the allowlist would silently
        leak that token into agent subprocesses again.
        """
        env = {
            "PATH": "/usr/bin",
            "HOME": "/home/agent",
            "PLATFORM_MCP_TOKEN": "tok-leaked",
        }
        result = self._build_with_env(env)
        assert "PLATFORM_MCP_TOKEN" not in result

    # -- Allowed prefix vars pass through ----------------------------------

    def test_allowed_prefix_vars_pass_through(self) -> None:
        env = {
            "LC_ALL": "en_US.UTF-8",
            "LC_CTYPE": "UTF-8",
            "GIT_AUTHOR_NAME": "Agent",
            "GIT_COMMITTER_EMAIL": "agent@example.com",
            "XDG_CONFIG_HOME": "/home/agent/.config",
        }
        result = self._build_with_env(env)
        for key, value in env.items():
            assert result[key] == value, f"{key} (prefix match) should pass through"

    # -- Blocked vars are excluded -----------------------------------------

    def test_blocked_exact_vars_excluded(self) -> None:
        env = {
            "AGENT_HARNESS_SERVICE_API_KEY": "secret-key",
            "X_API_KEY": "another-secret",
            "AHS_MCP_SECRET": "mcp-secret",
            "GATEWAY_BASE_URL": "https://internal.gateway",
        }
        result = self._build_with_env(env)
        for key in env:
            assert key not in result, f"{key} should be blocked"

    def test_blocked_prefix_vars_excluded(self) -> None:
        env = {
            "POSTGRES_PASSWORD": "db-pass",
            "POSTGRES_HOST": "db.internal",
            "POSTGRES_USER": "admin",
            "SLACK_BOT_TOKEN": "xoxb-123",
            "SLACK_APP_TOKEN": "xapp-456",
        }
        result = self._build_with_env(env)
        for key in env:
            assert key not in result, f"{key} should be blocked by prefix"

    def test_blocked_substring_vars_excluded(self) -> None:
        env = {
            "MY_PASSWORD_VAR": "pass123",
            "DB_SECRET_KEY": "sec456",
            "SOME_PASSWORD": "pass789",
            "ANOTHER_SECRET": "sec012",
        }
        result = self._build_with_env(env)
        for key in env:
            assert key not in result, f"{key} should be blocked by substring"

    def test_blocked_substring_case_insensitive(self) -> None:
        """Mixed-case keys containing blocked substrings are still blocked."""
        env = {
            "GIT_Secret_Token": "should-not-leak",
            "Some_Password_Var": "pass123",
        }
        result = self._build_with_env(env)
        for key in env:
            assert key not in result, f"{key} should be blocked (case-insensitive substring)"

    # -- Blocklist takes precedence over allowlist -------------------------

    def test_blocklist_overrides_allowlist(self) -> None:
        """A var matching the allowlist but containing a blocked substring is still blocked."""
        env = {"GIT_SECRET_TOKEN": "should-not-leak"}
        result = self._build_with_env(env)
        # GIT_ prefix matches allowlist, but SECRET substring triggers blocklist
        assert "GIT_SECRET_TOKEN" not in result

    # -- Unknown vars are excluded (allowlist behavior) --------------------

    def test_unknown_vars_excluded(self) -> None:
        env = {
            "DATABASE_URL": "postgres://...",
            "REDIS_URL": "redis://localhost",
            "INTERNAL_API_TOKEN": "tok-internal",
            "MY_CUSTOM_VAR": "whatever",
        }
        result = self._build_with_env(env)
        for key in env:
            assert key not in result, f"{key} (unknown) should be excluded by allowlist"

    # -- Mixed environment -------------------------------------------------

    def test_mixed_environment(self) -> None:
        """Realistic env with a mix of allowed, blocked, and unknown vars."""
        env = {
            # Allowed
            "PATH": "/usr/bin",
            "HOME": "/home/agent",
            "ANTHROPIC_API_KEY": "sk-ant-xxx",
            "LC_ALL": "en_US.UTF-8",
            "GIT_AUTHOR_NAME": "Agent",
            # Blocked
            "POSTGRES_PASSWORD": "db-pass",
            "AHS_MCP_SECRET": "mcp-secret",
            "SLACK_BOT_TOKEN": "xoxb-123",
            # Unknown
            "DATABASE_URL": "postgres://...",
            "REDIS_URL": "redis://localhost",
        }
        result = self._build_with_env(env)

        assert result["PATH"] == "/home/agent/.local/bin:/usr/bin"
        assert result["HOME"] == "/home/agent"
        assert result["ANTHROPIC_API_KEY"] == "sk-ant-xxx"
        assert result["LC_ALL"] == "en_US.UTF-8"
        assert result["GIT_AUTHOR_NAME"] == "Agent"

        assert "POSTGRES_PASSWORD" not in result
        assert "AHS_MCP_SECRET" not in result
        assert "SLACK_BOT_TOKEN" not in result
        assert "DATABASE_URL" not in result
        assert "REDIS_URL" not in result

    # -- Empty environment -------------------------------------------------

    def test_empty_environment(self) -> None:
        import os

        result = self._build_with_env({})
        # Even with empty env, ~/.local/bin is added to PATH (falls back to expanduser)
        expected_local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
        assert result == {"PATH": expected_local_bin}

    # -- PATH prepending behavior ------------------------------------------

    def test_local_bin_prepended_to_path(self) -> None:
        """~/.local/bin is prepended to PATH when not already present."""
        env = {"HOME": "/home/agent", "PATH": "/usr/bin:/usr/local/bin"}
        result = self._build_with_env(env)
        assert result["PATH"] == "/home/agent/.local/bin:/usr/bin:/usr/local/bin"

    def test_local_bin_not_duplicated(self) -> None:
        """If ~/.local/bin is already in PATH, it is not added again."""
        env = {"HOME": "/home/agent", "PATH": "/home/agent/.local/bin:/usr/bin"}
        result = self._build_with_env(env)
        assert result["PATH"] == "/home/agent/.local/bin:/usr/bin"

    def test_no_trailing_colon_when_path_empty(self) -> None:
        """When PATH is empty, the result should NOT have a trailing colon."""
        env = {"HOME": "/home/agent", "PATH": ""}
        result = self._build_with_env(env)
        assert result["PATH"] == "/home/agent/.local/bin"
        assert not result["PATH"].endswith(":")
