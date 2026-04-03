"""Tests for ypl.mono_server.manage.

Covers module import, CLI argument parsing, and the command handlers
(mocked so no live database is required).
"""

from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Import smoke tests
# ---------------------------------------------------------------------------


def test_manage_module_importable() -> None:
    import importlib

    mod = importlib.import_module("ypl.mono_server.manage")
    assert mod is not None


def test_manage_exposes_public_api() -> None:
    from ypl.mono_server import manage

    for attr in (
        "build_parser",
        "cmd_add_user",
        "cmd_list_users",
        "cmd_add_role",
        "cmd_create_mcp_token",
        "cmd_reset",
        "main",
    ):
        assert hasattr(manage, attr), f"Missing attribute: {attr}"


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


class TestBuildParser:
    def _parser(self) -> object:
        from ypl.mono_server.manage import build_parser

        return build_parser()

    def test_add_user_required_args(self) -> None:
        import pytest
        from ypl.mono_server.manage import build_parser

        parser = build_parser()
        # Missing --name should fail
        with pytest.raises(SystemExit):
            parser.parse_args(["add-user", "--email", "a@b.com"])

    def test_add_user_full_args(self) -> None:
        from ypl.mono_server.manage import build_parser

        parser = build_parser()
        args = parser.parse_args(["add-user", "--email", "a@b.com", "--name", "Alice", "--role", "ADMIN"])
        assert args.command == "add-user"
        assert args.email == "a@b.com"
        assert args.name == "Alice"
        assert args.role == "ADMIN"

    def test_add_user_role_optional(self) -> None:
        from ypl.mono_server.manage import build_parser

        parser = build_parser()
        args = parser.parse_args(["add-user", "--email", "a@b.com", "--name", "Alice"])
        assert args.role is None

    def test_list_users_default_limit(self) -> None:
        from ypl.mono_server.manage import build_parser

        parser = build_parser()
        args = parser.parse_args(["list-users"])
        assert args.command == "list-users"
        assert args.limit == 20

    def test_list_users_custom_limit(self) -> None:
        from ypl.mono_server.manage import build_parser

        parser = build_parser()
        args = parser.parse_args(["list-users", "--limit", "5"])
        assert args.limit == 5

    def test_add_role_args(self) -> None:
        from ypl.mono_server.manage import build_parser

        parser = build_parser()
        args = parser.parse_args(["add-role", "--email", "a@b.com", "--role", "ENGINEER"])
        assert args.command == "add-role"
        assert args.email == "a@b.com"
        assert args.role == "ENGINEER"

    def test_create_mcp_token_args(self) -> None:
        from ypl.mono_server.manage import build_parser

        parser = build_parser()
        args = parser.parse_args(["create-mcp-token", "--email", "dev@co.com", "--description", "My token"])
        assert args.command == "create-mcp-token"
        assert args.email == "dev@co.com"
        assert args.description == "My token"

    def test_create_mcp_token_default_description(self) -> None:
        from ypl.mono_server.manage import build_parser

        parser = build_parser()
        args = parser.parse_args(["create-mcp-token", "--email", "dev@co.com"])
        assert args.description == "Developer token"

    def test_reset_default_no_yes(self) -> None:
        from ypl.mono_server.manage import build_parser

        parser = build_parser()
        args = parser.parse_args(["reset"])
        assert args.yes is False

    def test_reset_with_yes_flag(self) -> None:
        from ypl.mono_server.manage import build_parser

        parser = build_parser()
        args = parser.parse_args(["reset", "--yes"])
        assert args.yes is True


# ---------------------------------------------------------------------------
# Command handler tests (mocked engine)
# ---------------------------------------------------------------------------


def _mock_engine() -> MagicMock:
    engine = MagicMock()
    engine.dispose = AsyncMock()
    return engine


class TestCmdAddUser:
    async def test_returns_zero_on_success(self) -> None:
        from ypl.mono_server.manage import cmd_add_user

        with (
            patch("ypl.mono_server.manage._get_async_engine", return_value=_mock_engine()),
            patch("ypl.mono_server.manage.create_user_with_role", new_callable=AsyncMock) as mock_create,
        ):
            mock_create.return_value = MagicMock()
            result = await cmd_add_user("a@b.com", "Alice", "ADMIN")

        assert result == 0

    async def test_returns_one_for_unknown_role(self) -> None:
        from ypl.mono_server.manage import cmd_add_user

        result = await cmd_add_user("a@b.com", "Alice", "SUPERUSER")
        assert result == 1

    async def test_returns_one_on_db_exception(self) -> None:
        from ypl.mono_server.manage import cmd_add_user

        with (
            patch("ypl.mono_server.manage._get_async_engine", return_value=_mock_engine()),
            patch("ypl.mono_server.manage.create_user_with_role", new_callable=AsyncMock) as mock_create,
        ):
            mock_create.side_effect = RuntimeError("DB error")
            result = await cmd_add_user("a@b.com", "Alice", None)

        assert result == 1


class TestCmdAddRole:
    async def test_returns_one_for_unknown_role(self) -> None:
        from ypl.mono_server.manage import cmd_add_role

        result = await cmd_add_role("a@b.com", "UNKNOWN_ROLE")
        assert result == 1

    async def test_returns_one_when_user_not_found(self) -> None:
        from ypl.mono_server.manage import cmd_add_role

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.first.return_value = None  # user not found
        mock_session.exec.return_value = mock_result

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.mono_server.manage._get_async_engine", return_value=_mock_engine()),
            patch("ypl.mono_server.manage.make_session_factory", return_value=MagicMock(return_value=mock_ctx)),
        ):
            result = await cmd_add_role("nobody@example.com", "ENGINEER")

        assert result == 1


class TestCmdCreateMcpToken:
    async def test_returns_zero_on_success(self) -> None:
        from ypl.mono_server.manage import cmd_create_mcp_token

        with (
            patch("ypl.mono_server.manage._get_async_engine", return_value=_mock_engine()),
            patch(
                "ypl.mono_server.manage.create_mcp_dev_token",
                new_callable=AsyncMock,
                return_value="yupp_dev_abc123",
            ),
        ):
            result = await cmd_create_mcp_token("dev@co.com", "Test token")

        assert result == 0

    async def test_returns_one_on_exception(self) -> None:
        from ypl.mono_server.manage import cmd_create_mcp_token

        with (
            patch("ypl.mono_server.manage._get_async_engine", return_value=_mock_engine()),
            patch(
                "ypl.mono_server.manage.create_mcp_dev_token",
                new_callable=AsyncMock,
                side_effect=RuntimeError("fail"),
            ),
        ):
            result = await cmd_create_mcp_token("dev@co.com", "Test token")

        assert result == 1


class TestCmdReset:
    async def test_returns_zero_with_yes_flag(self) -> None:
        from ypl.mono_server.manage import cmd_reset

        with (
            patch("ypl.mono_server.manage._get_async_engine", return_value=_mock_engine()),
            patch("ypl.mono_server.manage.seed_roles", new_callable=AsyncMock),
            patch("ypl.mono_server.manage.create_user_with_role", new_callable=AsyncMock),
        ):
            result = await cmd_reset(yes=True)

        assert result == 0

    async def test_returns_one_on_exception(self) -> None:
        from ypl.mono_server.manage import cmd_reset

        with (
            patch("ypl.mono_server.manage._get_async_engine", return_value=_mock_engine()),
            patch("ypl.mono_server.manage.seed_roles", new_callable=AsyncMock, side_effect=RuntimeError("oops")),
        ):
            result = await cmd_reset(yes=True)

        assert result == 1


class TestCmdListUsers:
    async def test_returns_zero_on_empty_db(self) -> None:
        from ypl.mono_server.manage import cmd_list_users

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.exec.return_value = mock_result

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.mono_server.manage._get_async_engine", return_value=_mock_engine()),
            patch("ypl.mono_server.manage.make_session_factory", return_value=MagicMock(return_value=mock_ctx)),
        ):
            result = await cmd_list_users(limit=5)

        assert result == 0

    async def test_returns_one_on_exception(self) -> None:
        from ypl.mono_server.manage import cmd_list_users

        with (
            patch("ypl.mono_server.manage._get_async_engine", return_value=_mock_engine()),
            patch("ypl.mono_server.manage.make_session_factory", side_effect=RuntimeError("db error")),
        ):
            result = await cmd_list_users()

        assert result == 1


# ---------------------------------------------------------------------------
# get_db_engine helper — structural test (no live DB)
# ---------------------------------------------------------------------------


def test_get_async_engine_returns_engine_instance() -> None:
    """_get_async_engine builds an AsyncEngine when settings are reachable."""
    from sqlalchemy.ext.asyncio import AsyncEngine
    from ypl.mono_server.manage import _get_async_engine

    # Patch settings.db_url_for to return a valid-looking (but fake) URL so
    # we don't need a real Postgres instance.
    fake_url = "postgresql+asyncpg://user:pass@localhost:5432/testdb"
    mock_settings = MagicMock()
    mock_settings.db_url_for.return_value = fake_url

    with patch("ypl.mono_server.manage.create_async_engine") as mock_create:
        mock_create.return_value = MagicMock(spec=AsyncEngine)
        with patch("ypl.backend.config.settings", mock_settings):
            engine = _get_async_engine()

    mock_create.assert_called_once_with(fake_url, pool_pre_ping=True)
    assert engine is not None
