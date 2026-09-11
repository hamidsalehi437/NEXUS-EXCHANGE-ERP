"""User, role-assignment and permission queries (PART 6, PART 41).

Effective permissions are computed **in SQL** rather than in Python: the query is the
single source of truth for "what may this user do", and it composes the three rules
from SECURITY.md §3 in one round trip:

1. every permission granted by any of the user's roles;
2. minus every explicit deny (``user_permissions.is_granted = FALSE``);
3. plus explicit grants, ignoring expired overrides.

``bool_and(granted)`` implements "an explicit deny always wins" without a second
query or a Python merge that could drift from the database.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.branch import Branch
from app.models.device import Device
from app.models.role import Permission, Role, RolePermission
from app.models.user import User, UserPermission, UserRole

# Deny-wins resolution of the effective permission set (see the module docstring).
_EFFECTIVE_PERMISSIONS_SQL = text(
    """
    WITH grants AS (
        SELECT rp.permission_code::text AS code, TRUE AS granted
          FROM user_roles ur
          JOIN role_permissions rp ON rp.role_id = ur.role_id
         WHERE ur.user_id = :user_id
        UNION
        SELECT up.permission_code::text AS code, up.is_granted
          FROM user_permissions up
         WHERE up.user_id = :user_id
           AND (up.expires_at IS NULL OR up.expires_at > now())
    )
    SELECT code, bool_and(granted) AS granted
      FROM grants
     GROUP BY code
     ORDER BY code
    """
)


class UserRepository:
    """Reads and writes for ``users`` and the identity tables around it."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ lookups
    async def get_by_id(self, user_id: uuid.UUID, *, for_update: bool = False) -> User | None:
        statement = select(User).where(User.id == user_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_username(self, username: str, *, for_update: bool = False) -> User | None:
        """Case-insensitive lookup matching the ``ux_users_username_lower`` index.

        A wrong password and an unknown username must be indistinguishable to the
        caller, so the lookup is deliberately exact on the normalised form.
        """
        statement = select(User).where(func.lower(User.username) == username.strip().lower())
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def username_exists(self, username: str) -> bool:
        result = await self._session.execute(
            select(func.count())
            .select_from(User)
            .where(func.lower(User.username) == username.strip().lower())
        )
        return int(result.scalar_one()) > 0

    async def email_exists(self, email: str) -> bool:
        result = await self._session.execute(
            select(func.count()).select_from(User).where(func.lower(User.email) == email.lower())
        )
        return int(result.scalar_one()) > 0

    async def list_users(
        self,
        *,
        is_active: bool | None = None,
        role_name: str | None = None,
        branch_id: uuid.UUID | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[User], int]:
        """Filtered, paginated user list plus the total for the list envelope.

        ``branch_id`` filters by *device* assignment, because the approved schema binds
        a person to a branch through the devices they use (``devices.branch_id``) — there
        is no ``users.branch_id`` column and this phase does not add one.
        """
        filters: list[ColumnElement[bool]] = []
        if is_active is not None:
            filters.append(User.is_active.is_(is_active))
        if role_name is not None:
            filters.append(
                User.id.in_(
                    select(UserRole.user_id)
                    .join(Role, Role.id == UserRole.role_id)
                    .where(func.upper(Role.name) == role_name.strip().upper())
                )
            )
        if branch_id is not None:
            filters.append(
                User.id.in_(select(Device.registered_by).where(Device.branch_id == branch_id))
            )
        if search:
            pattern = f"%{search.strip().lower()}%"
            filters.append(
                func.lower(User.username).like(pattern)
                | func.lower(User.full_name).like(pattern)
                | func.lower(func.coalesce(User.email, "")).like(pattern)
            )

        statement: Select[tuple[User]] = select(User).order_by(
            User.created_at.desc(), User.username
        )
        if filters:
            statement = statement.where(*filters)

        total = await self._session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = await self._session.execute(statement.limit(limit).offset(offset))
        return list(rows.scalars().all()), int(total or 0)

    # -------------------------------------------------------------------- writes
    def add(self, user: User) -> User:
        self._session.add(user)
        return user

    async def assign_roles(self, user_id: uuid.UUID, role_ids: Sequence[uuid.UUID]) -> None:
        """Replace the user's role assignments with exactly ``role_ids``.

        The flush matters: the session runs with ``autoflush=False``, and the caller
        (``UserService``) reads the user's authority back inside the same transaction to
        build the response. Without it the API would report the *previous* role set.
        """
        await self._session.execute(
            text("DELETE FROM user_roles WHERE user_id = :user_id"), {"user_id": user_id}
        )
        for role_id in role_ids:
            self._session.add(UserRole(user_id=user_id, role_id=role_id))
        await self._session.flush()

    async def set_permission_overrides(
        self, user_id: uuid.UUID, overrides: Sequence[dict[str, object]]
    ) -> None:
        """Replace the user's explicit grants/denies with exactly ``overrides``."""
        await self._session.execute(
            text("DELETE FROM user_permissions WHERE user_id = :user_id"), {"user_id": user_id}
        )
        for override in overrides:
            self._session.add(UserPermission(user_id=user_id, **override))
        # See assign_roles: the new overrides must be visible to the reads that follow.
        await self._session.flush()

    # ---------------------------------------------------------------- authorisation
    async def role_names(self, user_id: uuid.UUID) -> list[str]:
        rows = await self._session.execute(
            select(Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
        )
        return sorted(str(name) for name in rows.scalars().all())

    async def effective_permissions(self, user_id: uuid.UUID) -> frozenset[str]:
        """Resolve the effective permission set (deny wins) for ``user_id``."""
        result = await self._session.execute(_EFFECTIVE_PERMISSIONS_SQL, {"user_id": user_id})
        rows = result.all()
        return frozenset(str(code) for code, granted in rows if granted)

    async def permission_overrides(self, user_id: uuid.UUID) -> list[UserPermission]:
        rows = await self._session.execute(
            select(UserPermission)
            .where(UserPermission.user_id == user_id)
            .order_by(UserPermission.permission_code)
        )
        return list(rows.scalars().all())

    async def roles_by_names(self, names: Sequence[str]) -> list[Role]:
        """Resolve role names to rows, preserving the caller's order; unknown names drop out."""
        wanted = [name.strip().upper() for name in names]
        if not wanted:
            return []
        rows = await self._session.execute(select(Role).where(func.upper(Role.name).in_(wanted)))
        by_name = {str(role.name).upper(): role for role in rows.scalars().all()}
        return [by_name[name] for name in wanted if name in by_name]

    async def roles_by_ids(self, ids: Sequence[uuid.UUID]) -> list[Role]:
        if not ids:
            return []
        rows = await self._session.execute(select(Role).where(Role.id.in_(list(ids))))
        by_id = {role.id: role for role in rows.scalars().all()}
        return [by_id[role_id] for role_id in ids if role_id in by_id]


class RoleRepository:
    """Reads for ``roles``, ``permissions`` and ``role_permissions``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_roles(self) -> list[Role]:
        rows = await self._session.execute(select(Role).order_by(Role.name))
        return list(rows.scalars().all())

    async def get_role(self, role_id: uuid.UUID) -> Role | None:
        return (
            await self._session.execute(select(Role).where(Role.id == role_id))
        ).scalar_one_or_none()

    async def get_role_by_name(self, name: str) -> Role | None:
        return (
            await self._session.execute(
                select(Role).where(func.upper(Role.name) == name.strip().upper())
            )
        ).scalar_one_or_none()

    async def list_permissions(self) -> list[Permission]:
        rows = await self._session.execute(select(Permission).order_by(Permission.code))
        return list(rows.scalars().all())

    async def permission_codes(self) -> frozenset[str]:
        rows = await self._session.execute(select(Permission.code))
        return frozenset(str(code) for code in rows.scalars().all())

    async def role_permission_codes(self, role_id: uuid.UUID) -> frozenset[str]:
        rows = await self._session.execute(
            select(RolePermission.permission_code).where(RolePermission.role_id == role_id)
        )
        return frozenset(str(code) for code in rows.scalars().all())

    async def set_role_permissions(self, role_id: uuid.UUID, codes: Sequence[str]) -> None:
        """Replace a role's permission set with exactly ``codes``."""
        await self._session.execute(
            text("DELETE FROM role_permissions WHERE role_id = :role_id"), {"role_id": role_id}
        )
        for code in codes:
            self._session.add(RolePermission(role_id=role_id, permission_code=code))
        # See assign_roles: the endpoint returns the role's new permission set.
        await self._session.flush()


class BranchRepository:
    """Minimal branch reads needed to bind devices and sessions (full CRUD is Phase 3)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_branch(self, branch_id: uuid.UUID) -> Branch | None:
        return (
            await self._session.execute(select(Branch).where(Branch.id == branch_id))
        ).scalar_one_or_none()

    async def active_branches(self) -> list[Branch]:
        rows = await self._session.execute(
            select(Branch).where(Branch.is_active.is_(True)).order_by(Branch.code)
        )
        return list(rows.scalars().all())

    async def count_branches(self) -> int:
        return int(await self._session.scalar(select(func.count()).select_from(Branch)) or 0)

    def add(self, branch: Branch) -> Branch:
        self._session.add(branch)
        return branch


def now_utc() -> dt.datetime:
    """Single definition of "now" for the repository layer."""
    return dt.datetime.now(tz=dt.UTC)
