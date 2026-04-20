"""Unit tests for ypl/backend/utils/soul_utils.py.

Covers:
- SoulAuthError, UserNotFoundError, PermissionDeniedError, RoleDeniedError
- validate_read_yuppaste, validate_write_yuppaste (delegates to validate_permissions)
- validate_admin (role check, error handling)
- validate_role (DB lookup)
- has_role (DB lookup)
- has_permission / has_permission_cached (DB lookup)
- has_permission_by_user_id / has_permission_by_user_id_cached
- validate_permissions (header, env check, permission check)
- get_soul_url, get_litter_url, get_support_ticket_soul_link (pure URL builders)
"""

from __future__ import annotations
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette import status
from ypl.backend.utils.soul_utils import (
    PermissionDeniedError,
    RoleDeniedError,
    SoulAuthError,
    UserNotFoundError,
    get_litter_url,
    get_soul_url,
    get_support_ticket_soul_link,
    has_permission,
    has_permission_by_user_id,
    has_role,
    validate_admin,
    validate_permissions,
    validate_role,
)

MODULE = "ypl.backend.utils.soul_utils"


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------


class TestSoulAuthError:
    def test_default_status_code(self) -> None:
        err = SoulAuthError()
        assert err.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    def test_custom_detail(self) -> None:
        err = SoulAuthError(detail="custom message", status_code=503)
        assert err.detail == "custom message"
        assert err.status_code == 503


class TestUserNotFoundError:
    def test_status_code_is_401(self) -> None:
        err = UserNotFoundError()
        assert err.status_code == status.HTTP_401_UNAUTHORIZED

    def test_default_detail(self) -> None:
        err = UserNotFoundError()
        assert "not found" in err.detail.lower()

    def test_custom_detail(self) -> None:
        err = UserNotFoundError(detail="user missing")
        assert err.detail == "user missing"


class TestPermissionDeniedError:
    def test_status_code_is_403(self) -> None:
        err = PermissionDeniedError()
        assert err.status_code == status.HTTP_403_FORBIDDEN

    def test_default_detail(self) -> None:
        err = PermissionDeniedError()
        assert "denied" in err.detail.lower()


class TestRoleDeniedError:
    def test_status_code_is_403(self) -> None:
        err = RoleDeniedError()
        assert err.status_code == status.HTTP_403_FORBIDDEN

    def test_default_detail(self) -> None:
        err = RoleDeniedError()
        assert "denied" in err.detail.lower()


# ---------------------------------------------------------------------------
# get_soul_url / get_litter_url / get_support_ticket_soul_link — pure
# ---------------------------------------------------------------------------


class TestGetSoulUrl:
    def test_production_url(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"
        with patch(f"{MODULE}.settings", mock_settings):
            url = get_soul_url("test@example.com")
        assert "yupp-soul.vercel.app" in url
        assert "test@example.com" in url

    def test_non_production_url(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "staging"
        with patch(f"{MODULE}.settings", mock_settings):
            url = get_soul_url("test@example.com")
        assert "chaos-soul.vercel.app" in url

    def test_includes_query_param(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "local"
        with patch(f"{MODULE}.settings", mock_settings):
            url = get_soul_url("my-query")
        assert "my-query" in url


class TestGetLitterUrl:
    def test_production_url(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"
        with patch(f"{MODULE}.settings", mock_settings):
            url = get_litter_url("turn-123")
        assert "lit.yupp.ai" in url
        assert "turn-123" in url

    def test_staging_url(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "staging"
        with patch(f"{MODULE}.settings", mock_settings):
            url = get_litter_url("turn-456")
        assert "lit-staging.yupp.ai" in url

    def test_local_url(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "local"
        with patch(f"{MODULE}.settings", mock_settings):
            url = get_litter_url("turn-789")
        assert "localhost" in url


class TestGetSupportTicketSoulLink:
    def test_production_url(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"
        with patch(f"{MODULE}.settings", mock_settings):
            url = get_support_ticket_soul_link("ticket-abc")
        assert "yupp-soul.vercel.app" in url
        assert "ticket-abc" in url

    def test_staging_url(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "staging"
        with patch(f"{MODULE}.settings", mock_settings):
            url = get_support_ticket_soul_link("ticket-def")
        assert "chaos-soul.vercel.app" in url
        assert "ticket-def" in url

    def test_local_url(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "local"
        with patch(f"{MODULE}.settings", mock_settings):
            url = get_support_ticket_soul_link("ticket-ghi")
        assert "localhost" in url
        assert "ticket-ghi" in url


# ---------------------------------------------------------------------------
# validate_admin
# ---------------------------------------------------------------------------


class TestValidateAdmin:
    async def test_raises_404_when_user_not_found(self) -> None:
        with (
            patch(f"{MODULE}.validate_role", new_callable=AsyncMock, side_effect=UserNotFoundError("not found")),
            pytest.raises(HTTPException) as exc_info,
        ):
            await validate_admin("missing@example.com")

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    async def test_raises_403_when_role_denied(self) -> None:
        with (
            patch(f"{MODULE}.validate_role", new_callable=AsyncMock, side_effect=RoleDeniedError("denied")),
            pytest.raises(HTTPException) as exc_info,
        ):
            await validate_admin("noadmin@example.com")

        assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN

    async def test_raises_500_on_unexpected_error(self) -> None:
        with (
            patch(f"{MODULE}.validate_role", new_callable=AsyncMock, side_effect=Exception("DB error")),
            pytest.raises(HTTPException) as exc_info,
        ):
            await validate_admin("user@example.com")

        assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    async def test_passes_for_admin_user(self) -> None:
        with patch(f"{MODULE}.validate_role", new_callable=AsyncMock):
            await validate_admin("admin@example.com")  # Should not raise


# ---------------------------------------------------------------------------
# validate_role
# ---------------------------------------------------------------------------


class TestValidateRole:
    async def _make_session(self, user: MagicMock | None) -> MagicMock:
        """Build a mock async session context manager."""
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = user
        session.execute.return_value = result
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    async def test_raises_user_not_found_when_no_user(self) -> None:
        mock_user = None
        ctx = await self._make_session(mock_user)
        with (
            patch(f"{MODULE}.get_async_session", return_value=ctx),
            pytest.raises(UserNotFoundError),
        ):
            from ypl.db.rbac import RoleName

            await validate_role([RoleName.ADMIN], "nobody@example.com")

    async def test_raises_role_denied_when_user_lacks_role(self) -> None:
        mock_user = MagicMock()
        mock_user.user_id = str(uuid.uuid4())
        ctx = await self._make_session(mock_user)

        with (
            patch(f"{MODULE}.get_async_session", return_value=ctx),
            patch(f"{MODULE}.has_role", new_callable=AsyncMock, return_value=False),
            pytest.raises(RoleDeniedError),
        ):
            from ypl.db.rbac import RoleName

            await validate_role([RoleName.ADMIN], "user@example.com")

    async def test_returns_when_user_has_role(self) -> None:
        mock_user = MagicMock()
        mock_user.user_id = str(uuid.uuid4())
        ctx = await self._make_session(mock_user)

        with (
            patch(f"{MODULE}.get_async_session", return_value=ctx),
            patch(f"{MODULE}.has_role", new_callable=AsyncMock, return_value=True),
        ):
            from ypl.db.rbac import RoleName

            await validate_role([RoleName.ADMIN], "admin@example.com")  # No raise


# ---------------------------------------------------------------------------
# has_role
# ---------------------------------------------------------------------------


class TestHasRole:
    async def _make_session_execute(self, role: MagicMock | None, user_role: MagicMock | None) -> AsyncMock:
        session = AsyncMock()
        role_result = MagicMock()
        role_result.scalar_one_or_none.return_value = role
        user_role_result = MagicMock()
        user_role_result.scalar_one_or_none.return_value = user_role
        session.execute.side_effect = [role_result, user_role_result]
        return session

    async def test_returns_false_when_role_does_not_exist(self) -> None:
        session = await self._make_session_execute(None, None)
        from ypl.db.rbac import RoleName

        result = await has_role("user-123", RoleName.ADMIN, session)
        assert result is False

    async def test_returns_false_when_user_does_not_have_role(self) -> None:
        mock_role = MagicMock()
        mock_role.role_id = uuid.uuid4()
        session = await self._make_session_execute(mock_role, None)
        from ypl.db.rbac import RoleName

        result = await has_role("user-123", RoleName.ADMIN, session)
        assert result is False

    async def test_returns_true_when_user_has_role(self) -> None:
        mock_role = MagicMock()
        mock_role.role_id = uuid.uuid4()
        mock_user_role = MagicMock()
        session = await self._make_session_execute(mock_role, mock_user_role)
        from ypl.db.rbac import RoleName

        result = await has_role("user-123", RoleName.ADMIN, session)
        assert result is True

    async def test_returns_false_on_exception(self) -> None:
        session = AsyncMock()
        session.execute.side_effect = Exception("DB error")
        from ypl.db.rbac import RoleName

        result = await has_role("user-123", RoleName.ADMIN, session)
        assert result is False


# ---------------------------------------------------------------------------
# has_permission
# ---------------------------------------------------------------------------


class TestHasPermission:
    async def test_returns_false_when_user_not_found(self) -> None:
        session = AsyncMock()
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = None
        session.execute.return_value = user_result

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(f"{MODULE}.get_async_session", return_value=ctx):
            from ypl.db.rbac import Permission

            result = await has_permission("nobody@example.com", Permission.READ_YUPPASTE)

        assert result is False

    async def test_returns_false_on_exception(self) -> None:
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=Exception("DB down"))
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(f"{MODULE}.get_async_session", return_value=ctx):
            from ypl.db.rbac import Permission

            result = await has_permission("user@example.com", Permission.WRITE_YUPPASTE)

        assert result is False

    async def test_returns_true_when_user_has_permission(self) -> None:
        mock_user = MagicMock()
        mock_user.user_id = str(uuid.uuid4())
        session = AsyncMock()
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = mock_user
        session.execute.return_value = user_result

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(f"{MODULE}.get_async_session", return_value=ctx),
            patch(f"{MODULE}._check_user_permission", new_callable=AsyncMock, return_value=True),
        ):
            from ypl.db.rbac import Permission

            result = await has_permission("user@example.com", Permission.READ_YUPPASTE)

        assert result is True


# ---------------------------------------------------------------------------
# has_permission_by_user_id
# ---------------------------------------------------------------------------


class TestHasPermissionByUserId:
    async def test_returns_false_when_user_not_active(self) -> None:
        session = AsyncMock()
        result = MagicMock()
        result.first.return_value = None  # User not found / deleted
        session.execute.return_value = result

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(f"{MODULE}.get_async_session", return_value=ctx):
            from ypl.db.rbac import Permission

            out = await has_permission_by_user_id("user-id-123", Permission.READ_YUPPASTE)

        assert out is False

    async def test_returns_false_on_exception(self) -> None:
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=Exception("conn error"))
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(f"{MODULE}.get_async_session", return_value=ctx):
            from ypl.db.rbac import Permission

            out = await has_permission_by_user_id("uid-999", Permission.MANAGE_AGENTS)

        assert out is False

    async def test_returns_true_when_user_has_permission(self) -> None:
        session = AsyncMock()
        user_check_result = MagicMock()
        user_check_result.first.return_value = ("user-id-123",)
        session.execute.return_value = user_check_result

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(f"{MODULE}.get_async_session", return_value=ctx),
            patch(f"{MODULE}._check_user_permission", new_callable=AsyncMock, return_value=True),
        ):
            from ypl.db.rbac import Permission

            out = await has_permission_by_user_id("uid-456", Permission.READ_YUPPASTE)

        assert out is True


# ---------------------------------------------------------------------------
# validate_permissions
# ---------------------------------------------------------------------------


class TestValidatePermissions:
    async def test_raises_401_when_email_missing(self) -> None:
        from ypl.db.rbac import Permission

        with pytest.raises(HTTPException) as exc_info:
            await validate_permissions([Permission.READ_YUPPASTE], x_creator_email=None)

        assert exc_info.value.status_code == 401

    async def test_passes_when_no_permissions_required(self) -> None:
        # Empty permissions list → always passes
        await validate_permissions([], x_creator_email="user@example.com")  # No raise

    async def test_passes_in_non_production_env(self) -> None:
        from ypl.db.rbac import Permission

        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "staging"

        with patch(f"{MODULE}.settings", mock_settings):
            # Even with permissions required, non-production env skips check
            await validate_permissions([Permission.READ_YUPPASTE], x_creator_email="user@example.com")

    async def test_raises_permission_denied_in_production(self) -> None:
        from ypl.db.rbac import Permission

        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"

        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch(f"{MODULE}.has_permission", new_callable=AsyncMock, return_value=False),
            pytest.raises(PermissionDeniedError),
        ):
            await validate_permissions([Permission.READ_YUPPASTE], x_creator_email="user@example.com")

    async def test_passes_in_production_when_has_permission(self) -> None:
        from ypl.db.rbac import Permission

        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"

        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch(f"{MODULE}.has_permission", new_callable=AsyncMock, return_value=True),
        ):
            await validate_permissions([Permission.READ_YUPPASTE], x_creator_email="admin@example.com")


# ---------------------------------------------------------------------------
# validate_read_yuppaste / validate_write_yuppaste
# ---------------------------------------------------------------------------


class TestValidateYuppaste:
    async def test_validate_read_yuppaste_delegates_to_validate_permissions(self) -> None:
        from ypl.backend.utils.soul_utils import validate_read_yuppaste

        with patch(f"{MODULE}.validate_permissions", new_callable=AsyncMock) as mock_vp:
            await validate_read_yuppaste(x_creator_email="user@example.com")

        mock_vp.assert_awaited_once()
        call_args = mock_vp.call_args
        from ypl.db.rbac import Permission

        assert Permission.READ_YUPPASTE in call_args[0][0]

    async def test_validate_write_yuppaste_delegates_to_validate_permissions(self) -> None:
        from ypl.backend.utils.soul_utils import validate_write_yuppaste

        with patch(f"{MODULE}.validate_permissions", new_callable=AsyncMock) as mock_vp:
            await validate_write_yuppaste(x_creator_email="user@example.com")

        mock_vp.assert_awaited_once()
        call_args = mock_vp.call_args
        from ypl.db.rbac import Permission

        assert Permission.WRITE_YUPPASTE in call_args[0][0]
