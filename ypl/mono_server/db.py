"""Shared database helpers for the mono_server CLIs.

Extracted from ``setup.py`` so that both ``setup.py`` (the interactive wizard)
and ``manage.py`` (the management CLI) share a single, stable public API
instead of ``manage.py`` importing a private ``_``-prefixed symbol from the
wizard module.

This module is intentionally kept free of interactive I/O (rich prompts, etc.)
so it can be imported and unit-tested without side effects.
"""

from __future__ import annotations
import uuid

from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

import ypl.db.all_models  # noqa: F401 — register all SQLModel mappers before any query
from ypl.db.mcp import MCPDevToken, MCPTokenStatus
from ypl.db.rbac import Permission, Role, RoleName, RolePermission, UserRoleAssociation
from ypl.db.users import User, UserStatus, UserType
from ypl.mcp_server.auth_dev_token import generate_token, get_token_lookup_key, hash_token

console = Console()

# ---------------------------------------------------------------------------
# Role → Permission mapping
# ---------------------------------------------------------------------------

#: Canonical role-permission assignments for the three built-in roles.
ROLE_PERMISSIONS: dict[RoleName, list[Permission]] = {
    RoleName.ADMIN: list(Permission),
    RoleName.ENGINEER: [
        Permission.MANAGE_AGENTS,
        Permission.MANAGE_AGENT_SCHEDULES,
        Permission.MANAGE_AGENT_PROJECTS,
        Permission.MANAGE_AGENT_SESSIONS,
        Permission.CREATE_AGENT,
        Permission.USE_MCP,
        Permission.READ_YUPPASTE,
        Permission.WRITE_YUPPASTE,
    ],
    RoleName.MCP_USER: [Permission.USE_MCP],
}

ROLE_DESCRIPTIONS: dict[RoleName, str] = {
    RoleName.ADMIN: "Full administrative access — all permissions",
    RoleName.ENGINEER: "Agent operations and MCP tool access",
    RoleName.MCP_USER: "MCP tool usage only",
}

# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return an async session factory bound to *engine*."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def make_engine(db_url: str) -> AsyncEngine:
    """Create and return an async SQLAlchemy engine for *db_url*."""
    return create_async_engine(db_url, pool_pre_ping=True)


# ---------------------------------------------------------------------------
# Role seeding
# ---------------------------------------------------------------------------


async def seed_roles(engine: AsyncEngine) -> None:
    """Seed ADMIN, ENGINEER, and MCP_USER roles with their permissions.

    Idempotent — existing roles are kept; missing permissions are **added** to
    bring each role up to date with the canonical ``ROLE_PERMISSIONS`` mapping.

    Args:
        engine: Async SQLAlchemy engine connected to the agent database.
    """
    factory = make_session_factory(engine)
    async with factory() as session:
        for role_name, permissions in ROLE_PERMISSIONS.items():
            existing = (await session.exec(select(Role).where(Role.name == role_name))).first()

            if existing is None:
                # Brand-new role — create it with all expected permissions
                role = Role(name=role_name, description=ROLE_DESCRIPTIONS[role_name])
                session.add(role)
                await session.flush()  # obtain role_id before inserting permissions

                for perm in permissions:
                    session.add(RolePermission(role_id=role.role_id, permission=perm))

                console.print(f"  [green]Created role[/green] {role_name.value} ({len(permissions)} permissions)")
            else:
                # Role exists — reconcile any permissions added since initial setup
                existing_perms = (
                    await session.exec(select(RolePermission).where(RolePermission.role_id == existing.role_id))
                ).all()
                existing_perm_set = {rp.permission for rp in existing_perms}
                missing = [p for p in permissions if p not in existing_perm_set]

                if missing:
                    for perm in missing:
                        session.add(RolePermission(role_id=existing.role_id, permission=perm))
                    console.print(
                        f"  [green]Updated role[/green] {role_name.value} (+{len(missing)} missing permissions added)"
                    )
                else:
                    console.print(f"  [dim]Role {role_name.value} up-to-date — skipped.[/dim]")

        await session.commit()


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------


async def create_user_with_role(
    engine: AsyncEngine,
    *,
    email: str,
    name: str,
    user_type: UserType = UserType.HUMAN,
    role_name: RoleName | None = None,
) -> User:
    """Create a user and optionally assign a role.

    Idempotent — if a user with the given email already exists it is returned
    unchanged (role assignment is still applied if the association is absent).

    Args:
        engine:     Async engine connected to the agent database.
        email:      User's email address (used as the unique identifier).
        name:       Display name.
        user_type:  HUMAN, AGENT, or SYSTEM.
        role_name:  Role to assign, or None to skip role assignment.

    Returns:
        The persisted User instance.
    """
    factory = make_session_factory(engine)
    async with factory() as session:
        existing = (await session.exec(select(User).where(User.email == email.lower()))).first()

        if existing is None:
            user = User(
                user_id=str(uuid.uuid4()),
                name=name,
                email=email.lower(),
                status=UserStatus.ACTIVE,
                user_type=user_type,
            )
            session.add(user)
            await session.flush()
            console.print(f"  [green]Created user[/green] {email} ({user_type.value})")
        else:
            user = existing
            console.print(f"  [dim]User {email} already exists — skipped creation.[/dim]")

        if role_name is not None:
            role = (await session.exec(select(Role).where(Role.name == role_name))).first()
            if role is None:
                console.print(f"  [yellow]⚠ Role {role_name.value} not found — skipping assignment.[/yellow]")
            else:
                assoc_exists = (
                    await session.exec(
                        select(UserRoleAssociation).where(
                            UserRoleAssociation.user_id == user.user_id,
                            UserRoleAssociation.role_id == role.role_id,
                        )
                    )
                ).first()
                if assoc_exists is None:
                    session.add(UserRoleAssociation(user_id=user.user_id, role_id=role.role_id))
                    console.print(f"  [green]Assigned role[/green] {role_name.value} → {email}")

        await session.commit()
        await session.refresh(user)
        return user


# ---------------------------------------------------------------------------
# MCP token creation
# ---------------------------------------------------------------------------


async def create_mcp_dev_token(
    engine: AsyncEngine,
    *,
    email: str,
    description: str = "Initial dev token",
) -> str:
    """Create an MCP developer token and persist it to the database.

    Validates that the target user exists and has ``USE_MCP`` permission before
    minting the token, preventing issuance to accounts that should never have
    MCP access.

    Args:
        engine:      Async engine connected to the agent database.
        email:       Email address to associate with the token.
        description: Human-readable description for the token.

    Returns:
        The plaintext token string (``yupp_dev_*``). Store it now — it cannot
        be retrieved later.

    Raises:
        ValueError: If the user does not exist or lacks ``USE_MCP`` permission.
    """
    factory = make_session_factory(engine)
    async with factory() as session:
        # --- Policy check 1: user must exist ---
        user = (await session.exec(select(User).where(User.email == email.lower()))).first()
        if user is None:
            raise ValueError(f"No user found with email '{email}'. Create the user first.")

        # --- Policy check 2: user must have USE_MCP permission via a role ---
        user_role_rows = (
            await session.exec(select(UserRoleAssociation).where(UserRoleAssociation.user_id == user.user_id))
        ).all()

        if not user_role_rows:
            raise ValueError(
                f"User '{email}' has no roles assigned. Assign an ENGINEER or ADMIN role before issuing a token."
            )

        role_ids = [ur.role_id for ur in user_role_rows]
        mcp_perm = (
            await session.exec(
                select(RolePermission).where(
                    col(RolePermission.role_id).in_(role_ids),
                    RolePermission.permission == Permission.USE_MCP,
                )
            )
        ).first()

        if mcp_perm is None:
            raise ValueError(
                f"User '{email}' does not have USE_MCP permission. "
                "Assign an ENGINEER or ADMIN role before issuing a token."
            )

        # --- Mint the token ---
        token = generate_token()
        lookup_key = get_token_lookup_key(token)
        token_hash = hash_token(token)

        dev_token = MCPDevToken(
            email=email.lower(),
            token_lookup_key=lookup_key,
            token_hash=token_hash,
            description=description,
            status=MCPTokenStatus.ACTIVE,
        )
        session.add(dev_token)
        await session.commit()

    return token
