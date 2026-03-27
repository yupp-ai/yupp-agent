"""Compatibility shim: maps yupp-mind soul_rbac names to yupp-agent rbac.

All code should eventually import from ypl.db.rbac directly.
"""

from ypl.db.rbac import Permission as SoulPermission  # noqa: F401
from ypl.db.rbac import Role as SoulRole  # noqa: F401
from ypl.db.rbac import (
    RoleName,  # noqa: F401
    RolePermission,  # noqa: F401
)
from ypl.db.rbac import UserRoleAssociation as UserRole  # noqa: F401
