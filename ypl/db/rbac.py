"""Role-Based Access Control models for yupp-agent.

Renamed from soul_rbac.py — tables drop the `soul_` prefix:
  soul_roles → roles
  soul_role_permissions → role_permissions
  soul_user_roles → user_roles
"""

import enum
import uuid

from sqlalchemy import Column, PrimaryKeyConstraint, Text, UniqueConstraint
from sqlalchemy import Enum as sa_Enum
from sqlmodel import Field, Relationship, SQLModel

from ypl.db.base import BaseModel


class RoleName(str, enum.Enum):
    ADMIN = "ADMIN"
    READONLY = "READONLY"
    ADMIN_AGENT = "ADMIN_AGENT"
    ENGINEER = "ENGINEER"
    ARTIFACT_USER = "ARTIFACT_USER"
    MCP_USER = "MCP_USER"


class Permission(str, enum.Enum):
    # User management
    READ_USERS = "read_users"
    WRITE_USERS = "write_users"

    # RBAC management
    MANAGE_RBAC = "manage_rbac"

    # Artifacts (any type)
    READ_ARTIFACT = "READ_ARTIFACT"
    WRITE_ARTIFACT = "WRITE_ARTIFACT"

    # MCP
    USE_MCP = "USE_MCP"

    # Agents
    MANAGE_AGENTS = "MANAGE_AGENTS"
    MANAGE_AGENT_SCHEDULES = "MANAGE_AGENT_SCHEDULES"
    MANAGE_AGENT_PROJECTS = "MANAGE_AGENT_PROJECTS"
    MANAGE_AGENT_SESSIONS = "MANAGE_AGENT_SESSIONS"
    CREATE_AGENT = "CREATE_AGENT"


class RolePermission(SQLModel, table=True):
    """Association model for role-permission relationship."""

    __tablename__ = "role_permissions"

    role_id: uuid.UUID = Field(
        foreign_key="roles.role_id",
        nullable=False,
    )
    permission: Permission = Field(
        sa_column=Column(
            sa_Enum(Permission, name="permission_enum"),
            nullable=False,
        )
    )

    role: "Role" = Relationship(back_populates="role_permissions")

    __table_args__ = (PrimaryKeyConstraint("role_id", "permission", name="pk_role_permission"),)


class UserRoleAssociation(SQLModel, table=True):
    """Association model for user-role relationship."""

    __tablename__ = "user_roles"

    user_id: str = Field(
        foreign_key="users.user_id",
        nullable=False,
    )
    role_id: uuid.UUID = Field(
        foreign_key="roles.role_id",
        nullable=False,
    )

    __table_args__ = (PrimaryKeyConstraint("user_id", "role_id", name="pk_user_role"),)


class Role(BaseModel, table=True):
    """Model for storing user roles and their associated permissions."""

    __tablename__ = "roles"

    role_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)
    name: RoleName = Field(
        sa_column=Column(
            sa_Enum(RoleName, name="role_name_enum"),
            unique=True,
            nullable=False,
        )
    )
    description: str = Field(sa_type=Text, nullable=False)

    role_permissions: list[RolePermission] = Relationship(back_populates="role")

    __table_args__ = (UniqueConstraint("name", name="uq_roles_name"),)

    @property
    def permissions(self) -> list[Permission]:
        """Get list of permissions for this role."""
        return [rp.permission for rp in self.role_permissions]
