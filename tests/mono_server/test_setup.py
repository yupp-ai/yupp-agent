"""Tests for ypl.mono_server.setup and ypl.mono_server.db.

Covers pure helper functions, DB-seeding functions (mocked), and the module
import surface.  All tests run without a live database or Redis.
"""

from __future__ import annotations
import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.db.rbac import Permission, RoleName
from ypl.db.users import UserType
from ypl.mono_server.db import (
    ROLE_DESCRIPTIONS,
    ROLE_PERMISSIONS,
    create_mcp_dev_token,
    create_user_with_role,
    seed_roles,
)
from ypl.mono_server.setup import (
    build_async_db_url,
    build_postgres_connection_json,
    generate_env_content,
    generate_fernet_key,
    generate_secret,
)

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestGenerateSecret:
    def test_returns_nonempty_string(self) -> None:
        s = generate_secret()
        assert isinstance(s, str)
        assert len(s) > 0

    def test_two_calls_differ(self) -> None:
        assert generate_secret() != generate_secret()

    def test_url_safe_characters(self) -> None:
        s = generate_secret()
        # URL-safe base64 may contain A-Z a-z 0-9 - _
        assert re.match(r"^[A-Za-z0-9_\-]+$", s), f"Non-URL-safe chars in: {s}"


class TestGenerateFernetKey:
    def test_returns_valid_base64url(self) -> None:
        key = generate_fernet_key()
        assert isinstance(key, str)
        # Fernet key is 32 bytes → 44-char base64url (with padding)
        assert len(key) in (43, 44), f"Unexpected length {len(key)}"

    def test_two_calls_differ(self) -> None:
        assert generate_fernet_key() != generate_fernet_key()


class TestBuildPostgresConnectionJson:
    def test_valid_json_output(self) -> None:
        result = build_postgres_connection_json("pguser", "s3cr3t", "localhost:5432", "mydb")
        parsed = json.loads(result)
        assert parsed["user"] == "pguser"
        assert parsed["password"] == "s3cr3t"
        assert parsed["host"] == "localhost:5432"
        assert parsed["database"] == "mydb"

    def test_password_with_special_chars(self) -> None:
        # Passwords with special characters must survive JSON round-trip
        pw = 'p@$$w0rd!"#'
        result = build_postgres_connection_json("u", pw, "h:5432", "d")
        parsed = json.loads(result)
        assert parsed["password"] == pw


class TestBuildAsyncDbUrl:
    def test_default_port(self) -> None:
        url = build_async_db_url("user", "pass", "localhost", "db")
        assert "postgresql+asyncpg" in url
        assert "5432" in url
        assert "db" in url

    def test_explicit_port(self) -> None:
        url = build_async_db_url("user", "pass", "dbhost:5433", "mydb")
        assert "5433" in url
        assert "dbhost" in url

    def test_driver_scheme(self) -> None:
        url = build_async_db_url("user", "pass", "localhost:5432", "db")
        assert url.startswith("postgresql+asyncpg://")

    def test_database_in_url(self) -> None:
        url = build_async_db_url("u", "p", "localhost:5432", "yupp_agent")
        assert url.endswith("/yupp_agent")


class TestGenerateEnvContent:
    """Tests for generate_env_content (pure string function)."""

    _PARAMS: dict[str, str] = {
        "postgres_user": "pguser",
        "postgres_password": "pgpass",
        "postgres_host": "localhost:5432",
        "postgres_database": "yupp_agent",
        "redis_url": "redis://localhost:6379/1",
        "secret_key": "my_secret",
        "x_api_key": "my_x_api_key",
        "ahs_api_key": "my_ahs_key",
        "mcp_jwt_key": "my_jwt_key",
        "mcp_enc_key": "my_enc_key",
        "slack_enc_key": "my_slack_key",
        "base_url": "http://localhost:8090",
        "ahs_token_emails": "admin@example.com",
    }

    def test_contains_postgres_connection(self) -> None:
        content = generate_env_content(self._PARAMS)
        assert "POSTGRES_CONNECTION_AGENTDB=" in content
        assert "pguser" in content

    def test_contains_redis_url(self) -> None:
        content = generate_env_content(self._PARAMS)
        assert "REDIS_URL=redis://localhost:6379/1" in content

    def test_contains_secret_key(self) -> None:
        content = generate_env_content(self._PARAMS)
        assert "SECRET_KEY=my_secret" in content

    def test_contains_x_api_key(self) -> None:
        content = generate_env_content(self._PARAMS)
        assert "X_API_KEY=my_x_api_key" in content

    def test_contains_ahs_api_key(self) -> None:
        content = generate_env_content(self._PARAMS)
        assert "AGENT_HARNESS_SERVICE_API_KEY=my_ahs_key" in content

    def test_contains_base_url(self) -> None:
        content = generate_env_content(self._PARAMS)
        assert "http://localhost:8090" in content

    def test_contains_mcp_jwt_key(self) -> None:
        content = generate_env_content(self._PARAMS)
        assert "MCP_OAUTH_JWT_SIGNING_KEY=my_jwt_key" in content

    def test_contains_mcp_enc_key(self) -> None:
        content = generate_env_content(self._PARAMS)
        assert "MCP_OAUTH_STORAGE_ENCRYPTION_KEY=my_enc_key" in content

    def test_contains_slack_enc_key(self) -> None:
        content = generate_env_content(self._PARAMS)
        assert "SLACK_AGENT_GW_ENCRYPTION_KEY=my_slack_key" in content

    def test_default_db_agentdb(self) -> None:
        content = generate_env_content(self._PARAMS)
        assert "DEFAULT_DB=agentdb" in content

    def test_environment_selfhosted(self) -> None:
        content = generate_env_content(self._PARAMS)
        assert "ENVIRONMENT=selfhosted" in content

    def test_returns_string(self) -> None:
        content = generate_env_content(self._PARAMS)
        assert isinstance(content, str)

    def test_mcp_server_mode_dev_token(self) -> None:
        content = generate_env_content(self._PARAMS)
        assert "MCP_SERVER_MODE=DEV_TOKEN" in content

    def test_contains_ahs_token_emails(self) -> None:
        content = generate_env_content(self._PARAMS)
        assert "AHS_SERVICE_TOKEN_EMAILS=admin@example.com" in content

    def test_ahs_token_emails_defaults_empty(self) -> None:
        params = {k: v for k, v in self._PARAMS.items() if k != "ahs_token_emails"}
        content = generate_env_content(params)
        assert "AHS_SERVICE_TOKEN_EMAILS=" in content


# ---------------------------------------------------------------------------
# Role definitions
# ---------------------------------------------------------------------------


class TestRoleDefinitions:
    def test_admin_has_all_permissions(self) -> None:
        admin_perms = set(ROLE_PERMISSIONS[RoleName.ADMIN])
        all_perms = set(Permission)
        assert admin_perms == all_perms, f"Missing admin perms: {all_perms - admin_perms}"

    def test_engineer_has_agent_perms(self) -> None:
        eng_perms = set(ROLE_PERMISSIONS[RoleName.ENGINEER])
        agent_perms = {
            Permission.MANAGE_AGENTS,
            Permission.MANAGE_AGENT_SCHEDULES,
            Permission.MANAGE_AGENT_PROJECTS,
            Permission.MANAGE_AGENT_SESSIONS,
            Permission.CREATE_AGENT,
        }
        assert agent_perms.issubset(eng_perms)

    def test_engineer_has_mcp_perm(self) -> None:
        assert Permission.USE_MCP in ROLE_PERMISSIONS[RoleName.ENGINEER]

    def test_mcp_user_has_only_use_mcp(self) -> None:
        mcp_perms = ROLE_PERMISSIONS[RoleName.MCP_USER]
        assert mcp_perms == [Permission.USE_MCP]

    def test_all_required_roles_defined(self) -> None:
        for name in (RoleName.ADMIN, RoleName.ENGINEER, RoleName.MCP_USER):
            assert name in ROLE_PERMISSIONS, f"Missing role: {name}"
            assert name in ROLE_DESCRIPTIONS, f"Missing description: {name}"

    def test_descriptions_are_nonempty(self) -> None:
        for name, desc in ROLE_DESCRIPTIONS.items():
            assert desc, f"Empty description for {name}"


# ---------------------------------------------------------------------------
# DB operations (mocked engine / session)
# ---------------------------------------------------------------------------


def _mock_engine() -> MagicMock:
    """Build a MagicMock that quacks like an AsyncEngine."""
    engine = MagicMock()
    engine.dispose = AsyncMock()
    return engine


def _make_mock_session(*, existing_result: object = None) -> tuple[AsyncMock, list[object]]:
    """Build a mock AsyncSession and a list that captures all .add() calls.

    ``existing_result`` is what session.exec(...).first() returns.
    Pass ``None`` to simulate "record does not exist yet".
    Pass a MagicMock() to simulate "record already exists".
    """
    added: list[object] = []

    mock_result = MagicMock()
    mock_result.first.return_value = existing_result

    mock_session = AsyncMock()
    mock_session.exec.return_value = mock_result
    # session.add() must be a plain MagicMock so that calling it without await
    # does not produce an un-awaited coroutine warning.
    mock_session.add = MagicMock(side_effect=added.append)
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()

    return mock_session, added


def _make_mock_ctx(session: AsyncMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


class TestSeedRoles:
    async def test_seed_roles_creates_admin_engineer_mcp(self) -> None:
        """seed_roles should add three Role records (one per ROLE_PERMISSIONS entry)."""
        mock_session, added = _make_mock_session(existing_result=None)
        mock_ctx = _make_mock_ctx(mock_session)

        with patch("ypl.mono_server.db.make_session_factory", return_value=MagicMock(return_value=mock_ctx)):
            await seed_roles(_mock_engine())

        # Added items: 3 Role objects + sum of all permissions
        from ypl.db.rbac import Role, RolePermission

        role_items = [o for o in added if isinstance(o, Role)]
        perm_items = [o for o in added if isinstance(o, RolePermission)]
        assert len(role_items) == 3
        assert len(perm_items) == sum(len(p) for p in ROLE_PERMISSIONS.values())

    async def test_seed_roles_skips_existing(self) -> None:
        """seed_roles should not add anything when all roles and permissions already exist."""
        from ypl.db.rbac import RolePermission

        # Simulate every permission already present (covers all three roles)
        all_perms = list({p for perms in ROLE_PERMISSIONS.values() for p in perms})
        perm_mocks = [MagicMock(spec=RolePermission, permission=p) for p in all_perms]

        mock_result = MagicMock()
        mock_result.first.return_value = MagicMock(role_id="existing-role-id")
        mock_result.all.return_value = perm_mocks

        added: list[object] = []
        mock_session = AsyncMock()
        mock_session.exec.return_value = mock_result
        mock_session.add = MagicMock(side_effect=added.append)
        mock_session.commit = AsyncMock()

        mock_ctx = _make_mock_ctx(mock_session)
        with patch("ypl.mono_server.db.make_session_factory", return_value=MagicMock(return_value=mock_ctx)):
            await seed_roles(_mock_engine())

        # Nothing should have been added
        assert added == []


class TestCreateUserWithRole:
    async def test_creates_new_user(self) -> None:
        """A non-existing user should be inserted."""
        mock_session, added = _make_mock_session(existing_result=None)
        mock_ctx = _make_mock_ctx(mock_session)

        with patch("ypl.mono_server.db.make_session_factory", return_value=MagicMock(return_value=mock_ctx)):
            await create_user_with_role(
                _mock_engine(),
                email="alice@example.com",
                name="Alice",
                user_type=UserType.HUMAN,
                role_name=None,
            )

        from ypl.db.users import User

        user_items = [o for o in added if isinstance(o, User)]
        assert len(user_items) == 1
        assert user_items[0].email == "alice@example.com"

    async def test_lowercases_email(self) -> None:
        """Email must be stored in lower-case."""
        mock_session, added = _make_mock_session(existing_result=None)
        mock_ctx = _make_mock_ctx(mock_session)

        with patch("ypl.mono_server.db.make_session_factory", return_value=MagicMock(return_value=mock_ctx)):
            await create_user_with_role(
                _mock_engine(), email="Alice@EXAMPLE.COM", name="Alice", user_type=UserType.HUMAN
            )

        from ypl.db.users import User

        user_items = [o for o in added if isinstance(o, User)]
        assert user_items[0].email == "alice@example.com"

    async def test_skips_creation_for_existing_user(self) -> None:
        """If a user already exists, no new User should be inserted."""
        # Simulate user already present (exec().first() returns non-None)
        mock_session, added = _make_mock_session(existing_result=MagicMock(user_id="existing-id"))
        mock_ctx = _make_mock_ctx(mock_session)

        with patch("ypl.mono_server.db.make_session_factory", return_value=MagicMock(return_value=mock_ctx)):
            await create_user_with_role(
                _mock_engine(), email="alice@example.com", name="Alice", user_type=UserType.HUMAN
            )

        from ypl.db.users import User

        user_items = [o for o in added if isinstance(o, User)]
        assert user_items == []


def _make_mock_session_with_use_mcp() -> tuple[AsyncMock, list[object]]:
    """Build a mock session that passes all create_mcp_dev_token policy checks.

    Simulates: user exists, has a role, and that role has USE_MCP permission.
    exec() is called three times in that order.
    """
    added: list[object] = []

    # Call 1: user lookup
    user_result = MagicMock()
    user_result.first.return_value = MagicMock(user_id="user-id-123")

    # Call 2: UserRoleAssociation lookup
    role_assoc_result = MagicMock()
    role_assoc_result.all.return_value = [MagicMock(role_id="role-id-456")]

    # Call 3: RolePermission USE_MCP check
    perm_result = MagicMock()
    perm_result.first.return_value = MagicMock(permission=Permission.USE_MCP)

    mock_session = AsyncMock()
    mock_session.exec.side_effect = [user_result, role_assoc_result, perm_result]
    mock_session.add = MagicMock(side_effect=added.append)
    mock_session.commit = AsyncMock()

    return mock_session, added


class TestCreateMcpDevToken:
    async def test_returns_yupp_dev_prefixed_token(self) -> None:
        """Token must follow the ``yupp_dev_*`` format."""
        mock_session, _ = _make_mock_session_with_use_mcp()
        mock_ctx = _make_mock_ctx(mock_session)

        with patch("ypl.mono_server.db.make_session_factory", return_value=MagicMock(return_value=mock_ctx)):
            token = await create_mcp_dev_token(_mock_engine(), email="dev@example.com")

        assert token.startswith("yupp_dev_")

    async def test_persists_mcp_dev_token_record(self) -> None:
        """An MCPDevToken should be added to the session."""
        mock_session, added = _make_mock_session_with_use_mcp()
        mock_ctx = _make_mock_ctx(mock_session)

        with patch("ypl.mono_server.db.make_session_factory", return_value=MagicMock(return_value=mock_ctx)):
            await create_mcp_dev_token(_mock_engine(), email="dev@example.com", description="test token")

        from ypl.db.mcp import MCPDevToken

        token_items = [o for o in added if isinstance(o, MCPDevToken)]
        assert len(token_items) == 1
        assert token_items[0].email == "dev@example.com"
        assert token_items[0].description == "test token"

    async def test_token_uniqueness(self) -> None:
        """Two consecutive token-creation calls should produce different tokens."""
        tokens: list[str] = []

        for _ in range(2):
            mock_session, _ = _make_mock_session_with_use_mcp()
            mock_ctx = _make_mock_ctx(mock_session)
            with patch("ypl.mono_server.db.make_session_factory", return_value=MagicMock(return_value=mock_ctx)):
                tokens.append(await create_mcp_dev_token(_mock_engine(), email="x@y.com"))

        assert tokens[0] != tokens[1]

    async def test_raises_for_missing_user(self) -> None:
        """create_mcp_dev_token raises ValueError when user does not exist."""
        mock_session, _ = _make_mock_session(existing_result=None)
        mock_ctx = _make_mock_ctx(mock_session)

        with (
            patch("ypl.mono_server.db.make_session_factory", return_value=MagicMock(return_value=mock_ctx)),
            pytest.raises(ValueError, match="No user found"),
        ):
            await create_mcp_dev_token(_mock_engine(), email="nobody@example.com")

    async def test_raises_for_missing_use_mcp_permission(self) -> None:
        """create_mcp_dev_token raises ValueError when user lacks USE_MCP permission."""
        user_result = MagicMock()
        user_result.first.return_value = MagicMock(user_id="user-id-123")

        role_assoc_result = MagicMock()
        role_assoc_result.all.return_value = [MagicMock(role_id="role-id-456")]

        no_perm_result = MagicMock()
        no_perm_result.first.return_value = None  # no USE_MCP permission found

        mock_session = AsyncMock()
        mock_session.exec.side_effect = [user_result, role_assoc_result, no_perm_result]
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()

        mock_ctx = _make_mock_ctx(mock_session)
        with (
            patch("ypl.mono_server.db.make_session_factory", return_value=MagicMock(return_value=mock_ctx)),
            pytest.raises(ValueError, match="USE_MCP"),
        ):
            await create_mcp_dev_token(_mock_engine(), email="noperm@example.com")


# ---------------------------------------------------------------------------
# Module import smoke tests
# ---------------------------------------------------------------------------


def test_setup_module_importable() -> None:
    """ypl.mono_server.setup is importable without errors."""
    import importlib

    mod = importlib.import_module("ypl.mono_server.setup")
    assert mod is not None


def test_setup_exposes_public_api() -> None:
    """Key public symbols are exposed at module level on setup."""
    from ypl.mono_server import setup

    for attr in (
        "generate_secret",
        "generate_fernet_key",
        "build_postgres_connection_json",
        "build_async_db_url",
        "generate_env_content",
        # Re-exported from db.py:
        "seed_roles",
        "create_user_with_role",
        "create_mcp_dev_token",
        "ROLE_PERMISSIONS",
        "ROLE_DESCRIPTIONS",
    ):
        assert hasattr(setup, attr), f"Missing attribute: {attr}"


def test_db_module_importable() -> None:
    """ypl.mono_server.db is importable without errors."""
    import importlib

    mod = importlib.import_module("ypl.mono_server.db")
    assert mod is not None
