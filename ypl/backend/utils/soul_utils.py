from typing import cast
from uuid import UUID

from fastapi import Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from ypl.backend.config import settings
from ypl.backend.db import get_async_session
from ypl.backend.utils.json import json_dumps
from ypl.backend.utils.redis_utils import redis_async_cache
from ypl.db.rbac import Permission, Role, RoleName, RolePermission, UserRoleAssociation
from ypl.db.users import User
from ypl.structured_logger import get_logger

logger = get_logger()


class SoulAuthError(HTTPException):
    """Base exception for soul authentication and authorization errors."""

    def __init__(
        self,
        detail: str = "Soul authentication and authorization error",
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
    ):
        super().__init__(status_code=status_code, detail=detail)


class UserNotFoundError(SoulAuthError):
    """Exception raised when a user is not found."""

    def __init__(self, detail: str = "User not found"):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


class PermissionDeniedError(SoulAuthError):
    """Exception raised when a user doesn't have required permissions."""

    def __init__(self, detail: str = "Permission denied"):
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


class RoleDeniedError(SoulAuthError):
    """Exception raised when a user doesn't have required roles."""

    def __init__(self, detail: str = "Role denied"):
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


async def validate_read_yuppaste(x_creator_email: str = Header(..., alias="X-Creator-Email")) -> None:
    """Validate that the user has READ_YUPPASTE permission."""
    await validate_permissions([Permission.READ_YUPPASTE], x_creator_email)


async def validate_write_yuppaste(x_creator_email: str = Header(..., alias="X-Creator-Email")) -> None:
    """Validate that the user has WRITE_YUPPASTE permission."""
    await validate_permissions([Permission.WRITE_YUPPASTE], x_creator_email)


async def validate_admin(x_creator_email: str = Header(..., alias="X-Creator-Email")) -> None:
    """Validate that the user has ADMIN role.

    Args:
        x_creator_email: Email of the user to validate, passed via X-Creator-Email header

    Raises:
        HTTPException: If user is not found or doesn't have ADMIN role
    """
    try:
        await validate_role([RoleName.ADMIN], x_creator_email)
    except UserNotFoundError as e:
        # Let it raise 404 instead of the default 401.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except RoleDeniedError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User does not have access") from e
    except Exception as e:
        logger.warning("Error validating admin role", x_creator_email=x_creator_email, exc_info=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to validate admin role"
        ) from e


async def validate_role(role_names: list[RoleName], x_creator_email: str) -> None:
    """Validate that the user has at least one of the specified roles."""

    async with get_async_session() as session:
        user_stmt = select(User).where(
            User.email == x_creator_email,
            col(User.deleted_at).is_(None),
        )
        result = await session.execute(user_stmt)
        user = result.scalar_one_or_none()
        if not user:
            raise UserNotFoundError("Creator user not found")

        for role_name in role_names:
            if await has_role(user.user_id, role_name, session):
                return

        raise RoleDeniedError(f"User does not have any of the required roles: {[r.value for r in role_names]}")


async def has_role(user_id: str, role_name: RoleName, session: AsyncSession) -> bool:
    """Check if a user has a specific role.

    Args:
        user_id: The ID of the user to check
        role_name: The name of the role to check for
        session: The database session to use

    Returns:
        bool: True if the user has the role, False otherwise
    """
    try:
        role_stmt = select(Role).where(Role.name == role_name)
        role_result = await session.execute(role_stmt)
        role = role_result.scalar_one_or_none()

        if not role:
            return False

        user_role_stmt = select(UserRoleAssociation).where(
            UserRoleAssociation.user_id == user_id,
            UserRoleAssociation.role_id == role.role_id,
        )
        user_role_result = await session.execute(user_role_stmt)
        return user_role_result.scalar_one_or_none() is not None

    except Exception as e:
        logger.error("Error checking user role", exc_info=e)
        return False


@redis_async_cache(ttl_seconds=60 * 5)  # 5 minutes
async def has_permission_cached(creator_user_email: str, permission: Permission) -> bool:
    """
    Check if a user has a specific permission based on their email.
    """
    return await has_permission(creator_user_email, permission)


async def has_permission(creator_user_email: str, permission: Permission) -> bool:
    """
    Check if a user has a specific permission based on their email.

    Args:
        session: The database session
        creator_user_email: Email of the user to check permissions for
        permission: The permission to check

    Returns:
        bool: True if user has the permission, False else
    """
    try:
        async with get_async_session() as session:
            user_stmt = select(User).where(
                User.email == creator_user_email,
                col(User.deleted_at).is_(None),
            )
            result = await session.execute(user_stmt)
            user = result.scalar_one_or_none()
            if not user:
                return False

            return await _check_user_permission(session, user.user_id, permission)

    except Exception as e:
        log_dict = {
            "message": "Error checking user permission",
            "creator_user_email": creator_user_email,
            "permission": permission.value,
            "error": str(e),
        }
        logger.warning(json_dumps(log_dict))
        return False


@redis_async_cache(ttl_seconds=60 * 5)  # 5 minutes
async def has_permission_by_user_id_cached(user_id: str, permission: Permission) -> bool:
    """Check if a user has a specific permission based on their user_id."""
    return await has_permission_by_user_id(user_id, permission)


async def has_permission_by_user_id(user_id: str, permission: Permission) -> bool:
    """Check if a user has a specific permission based on their user_id.

    Verifies the user is active (not deleted) before checking roles → permissions.

    Args:
        user_id: The Yupp user ID
        permission: The permission to check

    Returns:
        bool: True if user has the permission, False otherwise
    """
    try:
        async with get_async_session() as session:
            # Verify user is active (not soft-deleted)
            user_stmt = select(User.user_id).where(
                User.user_id == user_id,
                col(User.deleted_at).is_(None),
            )
            if not (await session.execute(user_stmt)).first():
                return False
            return await _check_user_permission(session, user_id, permission)
    except Exception as e:
        log_dict = {
            "message": "Error checking user permission by user_id",
            "user_id": user_id,
            "permission": permission.value,
            "error": str(e),
        }
        logger.warning(json_dumps(log_dict))
        return False


async def _check_user_permission(session: AsyncSession, user_id: str, permission: Permission) -> bool:
    """Check if a user has a specific permission given their user_id and an open DB session."""
    # Get all roles for the user
    roles_stmt = select(UserRoleAssociation.role_id).where(UserRoleAssociation.user_id == user_id)
    role_results = await session.execute(roles_stmt)
    user_role_ids: list[UUID] = [cast(UUID, r[0]) for r in role_results]
    if not user_role_ids:
        return False

    # Check if any of the user's roles have the required permission
    permission_stmt = select(RolePermission).where(
        col(RolePermission.role_id).in_(user_role_ids),
        RolePermission.permission == permission,
    )
    return (await session.execute(permission_stmt)).first() is not None


async def validate_permissions(
    permissions: list[Permission],
    x_creator_email: str | None = Header(None, alias="X-Creator-Email"),
) -> None:
    """
    FastAPI dependency to validate permissions.
    Gets creator email from X-Creator-Email header and required permissions from security scopes.

    Usage:
        # For user management endpoints
        @router.get(
            "/users",
            dependencies=[Depends(validate_permissions)],
            security=[SecurityScopes(["READ_USERS"])]
        )

        # For payment management endpoints
        @router.post(
            "/payment-instruments",
            dependencies=[Depends(validate_permissions)],
            security=[SecurityScopes(["MANAGE_PAYMENT_INSTRUMENTS"])]
        )

        # For cashout management endpoints
        @router.post(
            "/cashout/approve",
            dependencies=[Depends(validate_permissions)],
            security=[SecurityScopes(["MANAGE_CASHOUT"])]
        )

    Args:
        security_scopes: Security scopes containing required permissions
        x_creator_email: Email of the user to validate, passed via X-Creator-Email header

    Raises:
        HTTPException: If creator email is missing or user doesn't have required permissions
    """
    if not x_creator_email:
        raise HTTPException(status_code=401, detail="X-Creator-Email header is required")

    if not permissions or settings.ENVIRONMENT != "production":
        # No permissions required
        return

    # Convert scope strings to Permission enums
    required_permissions = [Permission(permission) for permission in permissions]

    for permission in required_permissions:
        if not await has_permission(x_creator_email, permission):
            log_dict = {
                "message": "Permission denied",
                "creator_user_email": x_creator_email,
                "required_permission": permission.value,
            }
            logger.warning(json_dumps(log_dict))
            raise PermissionDeniedError(f"Permission denied. Required permission: {permission.value}")
