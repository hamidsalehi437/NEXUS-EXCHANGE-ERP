"""Seed roles and the permission matrix (PART 41, PART 45).

* ``permissions`` — the catalogue from :mod:`app.core.permissions`.
* ``roles`` — the six roles named by the master prompt; ``SUPER_ADMIN`` is flagged
  ``is_system`` so operators cannot remove the role that administers the system.
* ``role_permissions`` — the documented default matrix.

This seed is **authoritative for the seeded roles**: a grant that is no longer in
the matrix is removed, so the deployed RBAC always matches the reviewed code.
Per-user exceptions (``user_permissions``) are operational data and are never
touched.
"""

from __future__ import annotations

from sqlalchemy import delete, select

from app.core.permissions import (
    PERMISSION_DESCRIPTIONS,
    ROLE_DESCRIPTIONS,
    ROLE_PERMISSIONS,
    SYSTEM_ROLES,
    RoleName,
)
from app.core.permissions import (
    Permission as PermissionCode,
)
from app.models.role import Permission, Role, RolePermission
from seeds.base import SeedContext, SeedCounts, sync_rows


def run(context: SeedContext) -> SeedCounts:
    """Upsert permissions, roles and the default role → permission grants."""
    counts = SeedCounts()

    # --- permissions ---------------------------------------------------------
    counts.merge(
        sync_rows(
            context,
            Permission,
            natural_key=("code",),
            rows=[
                {"code": str(permission), "description": PERMISSION_DESCRIPTIONS[permission]}
                for permission in PermissionCode
            ],
            managed_fields=("description",),
        )
    )

    # --- roles --------------------------------------------------------------
    counts.merge(
        sync_rows(
            context,
            Role,
            natural_key=("name",),
            rows=[
                {
                    "name": str(role),
                    "description": ROLE_DESCRIPTIONS[role],
                    "is_system": role in SYSTEM_ROLES,
                }
                for role in RoleName
            ],
            managed_fields=("description", "is_system"),
        )
    )

    # Flush so the role rows exist for the permission mapping below (and for a
    # dry run, which reads back what a real run would have written before rolling back).
    context.session.flush()

    roles_by_name = {
        role.name: role for role in context.session.execute(select(Role)).scalars().all()
    }
    existing_grants = {
        (row.role_id, row.permission_code)
        for row in context.session.execute(select(RolePermission)).scalars().all()
    }

    desired_grants: set[tuple[object, str]] = set()
    for role_name, permissions in ROLE_PERMISSIONS.items():
        role = roles_by_name.get(str(role_name))
        if role is None:  # pragma: no cover - the role was just upserted
            raise RuntimeError(f"role {role_name} missing after upsert")
        desired_grants.update((role.id, str(permission)) for permission in permissions)

    for role_id, permission_code in sorted(desired_grants - existing_grants, key=str):
        context.session.add(RolePermission(role_id=role_id, permission_code=permission_code))
        counts.inserted += 1

    counts.unchanged += len(desired_grants & existing_grants)

    stale_grants = existing_grants - desired_grants
    for role_id, permission_code in sorted(stale_grants, key=str):
        context.session.execute(
            delete(RolePermission).where(
                RolePermission.role_id == role_id,
                RolePermission.permission_code == permission_code,
            )
        )
        counts.removed += 1

    if counts.changed:
        context.record_audit(
            action="SEED_ROLES_PERMISSIONS_APPLIED",
            entity_type="role",
            details={
                "roles": [str(role) for role in RoleName],
                "permissions": len(PERMISSION_DESCRIPTIONS),
                **counts.as_dict(),
            },
        )

    return counts
