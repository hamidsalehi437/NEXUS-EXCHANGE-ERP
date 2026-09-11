"""User, role and permission administration (PART 6, PART 25, PART 41).

Rules enforced here, all of them testable through the API:

* **No hard delete.** ``DELETE /users/{id}`` deactivates (``is_active = FALSE``); the
  database forbids the row from disappearing (PART 25) and the API never tries.
* **No privilege escalation by editing.** An administrator may only grant permissions
  they hold themselves. A manager with ``users.manage`` in a future role matrix cannot
  promote themselves to super admin through the role editor.
* **No self-lockout.** The caller cannot deactivate their own account or remove
  ``users.manage`` from their own role set — the two mistakes that would otherwise need
  database-level recovery.
* **Deactivation ends sessions.** Deactivating a user or changing their roles revokes
  their live sessions, so a revocation is immediate even for an access token already in
  the wild.
* **Every change is audited with a before/after diff** (``old_data``/``new_data``).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_actions import AuditAction
from app.core.config import Settings
from app.core.database import Database
from app.core.exceptions import (
    DuplicateResourceError,
    NexusError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ValidationError,
)
from app.core.permissions import SYSTEM_ROLES, RoleName
from app.core.permissions import Permission as PermissionCode
from app.core.security import PasswordHasher, PasswordPolicyError, enforce_password_policy
from app.models.role import Role
from app.models.user import User
from app.repositories.sessions import REASON_USER_DEACTIVATED, SessionRepository
from app.repositories.users import RoleRepository, UserRepository
from app.services.audit_service import ActorContext, AuditService


@dataclass(frozen=True, slots=True)
class UserProfile:
    """A user as the API presents it: identity, roles, overrides — never a hash."""

    user: User
    roles: list[str]
    permissions: frozenset[str]
    overrides: list[dict[str, object]]


@dataclass(frozen=True, slots=True)
class CreatedUser:
    """Result of creating a user."""

    user: User
    roles: list[str]


@dataclass(frozen=True, slots=True)
class PermissionOverride:
    """One explicit grant or deny for a user."""

    permission_code: str
    is_granted: bool
    expires_at: dt.datetime | None = None
    reason: str | None = None


class UserService:
    """Create, read and update users, their roles and their explicit permissions."""

    def __init__(self, *, database: Database, settings: Settings, hasher: PasswordHasher) -> None:
        self._database = database
        self._settings = settings
        self._hasher = hasher

    @staticmethod
    def _now() -> dt.datetime:
        return dt.datetime.now(tz=dt.UTC)

    # -------------------------------------------------------------------- reads
    async def get_profile(self, user_id: uuid.UUID) -> UserProfile:
        async with self._database.session() as session:
            users = UserRepository(session)
            user = await users.get_by_id(user_id)
            if user is None:
                raise ResourceNotFoundError("No such user.")
            return await self._profile(session, user)

    async def list_users(
        self,
        *,
        is_active: bool | None,
        role_name: str | None,
        branch_id: uuid.UUID | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[UserProfile], int]:
        async with self._database.session() as session:
            users = UserRepository(session)
            rows, total = await users.list_users(
                is_active=is_active,
                role_name=role_name,
                branch_id=branch_id,
                search=search,
                limit=limit,
                offset=offset,
            )
            profiles = [await self._profile(session, user) for user in rows]
        return profiles, total

    async def _profile(self, session: AsyncSession, user: User) -> UserProfile:
        users = UserRepository(session)
        overrides = await users.permission_overrides(user.id)
        return UserProfile(
            user=user,
            roles=await users.role_names(user.id),
            permissions=await users.effective_permissions(user.id),
            overrides=[
                {
                    "permission_code": override.permission_code,
                    "is_granted": override.is_granted,
                    "expires_at": override.expires_at,
                    "reason": override.reason,
                }
                for override in overrides
            ],
        )

    async def _record_refusal(
        self, error: NexusError, *, actor: ActorContext, attempted_roles: Sequence[str]
    ) -> None:
        """Persist a security refusal in its own transaction.

        The caller's transaction is about to be rolled back (the request fails), so the
        evidence is written *before* the exception leaves the service — a refused
        escalation that leaves no trace would be an auditability defect.
        """
        details = dict(error.details or {})
        async with self._database.transaction() as session:
            AuditService(session).record(
                action=AuditAction.SECURITY_PRIVILEGE_ESCALATION_BLOCKED,
                entity_type="role" if "role" in details else "user",
                entity_id=actor.user_id,
                new_data={
                    "attempted_roles": sorted({name.upper() for name in attempted_roles}),
                    "reason": details.get("reason", "PERMISSION_ESCALATION"),
                    **details,
                },
                actor=actor,
            )

    async def _authorise_roles(
        self, role_names: Sequence[str] | None, *, actor: ActorContext
    ) -> None:
        """Check a role assignment *before* the write transaction (see _record_refusal)."""
        if role_names is None:
            return
        async with self._database.session() as session:
            try:
                await self._resolve_roles(RoleRepository(session), role_names, actor=actor)
            except NexusError as error:
                if isinstance(error, PermissionDeniedError):
                    await self._record_refusal(
                        error, actor=actor, attempted_roles=role_names
                    )
                raise

    async def _authorise_permissions(
        self,
        codes: Sequence[str],
        *,
        actor: ActorContext,
        path: str,
        detail: dict[str, object] | None = None,
    ) -> None:
        """Check the codes an administrator is about to hand out, before writing.

        Order matters for a good error: an unknown code is a ``422`` (the client sent
        something the catalogue does not contain), while a known code the caller does not
        hold is a ``403`` that is audited as an attempted escalation.
        """
        async with self._database.session() as session:
            catalogue = await RoleRepository(session).permission_codes()
        unknown = sorted(set(codes) - catalogue)
        if unknown:
            raise ValidationError(
                "Unknown permission code.",
                details={"unknown": unknown, "hint": "Use GET /api/v1/permissions."},
            )
        escalation = sorted(set(codes) - set(actor.permissions))
        if escalation:
            error = PermissionDeniedError(
                "You cannot grant permissions that you do not hold yourself.",
                details={
                    "missing_permissions": escalation,
                    "reason": "PERMISSION_ESCALATION",
                    "path": path,
                    **(detail or {}),
                },
            )
            await self._record_refusal(error, actor=actor, attempted_roles=())
            raise error

    # ------------------------------------------------------------------- writes
    async def create_user(
        self,
        *,
        username: str,
        password: str,
        full_name: str,
        email: str | None,
        phone: str | None,
        role_names: Sequence[str],
        must_change_password: bool,
        actor: ActorContext,
    ) -> CreatedUser:
        """Create an account with the policy-checked password and initial roles."""
        await self._authorise_roles(role_names, actor=actor)
        async with self._database.transaction() as session:
            users = UserRepository(session)
            roles = RoleRepository(session)
            audit = AuditService(session)

            normalised = username.strip().lower()
            if await users.username_exists(normalised):
                raise DuplicateResourceError(
                    "That username is already taken.",
                    details={"fields": [{"field": "username", "code": "duplicate"}]},
                )
            if email and await users.email_exists(email):
                raise DuplicateResourceError(
                    "That e-mail address is already in use.",
                    details={"fields": [{"field": "email", "code": "duplicate"}]},
                )

            resolved_roles = await self._resolve_roles(roles, role_names, actor=actor)
            try:
                enforce_password_policy(password, username=normalised, full_name=full_name)
            except PasswordPolicyError as exc:
                raise ValidationError(
                    str(exc),
                    details={"fields": [{"field": "password", "code": "policy"}]},
                ) from exc

            user = User(
                username=normalised,
                full_name=full_name.strip(),
                email=email.lower() if email else None,
                phone=phone,
                password_hash=self._hasher.hash(password),
                is_active=True,
                must_change_password=must_change_password,
            )
            users.add(user)
            await session.flush()
            await users.assign_roles(user.id, [role.id for role in resolved_roles])

            audit.record(
                action=AuditAction.USER_CREATED,
                entity_type="user",
                entity_id=user.id,
                new_data={
                    "username": user.username,
                    "full_name": user.full_name,
                    "email": user.email,
                    "roles": sorted(role.name for role in resolved_roles),
                    "must_change_password": must_change_password,
                    "is_active": True,
                },
                actor=actor,
            )
            return CreatedUser(user=user, roles=sorted(role.name for role in resolved_roles))

    async def update_user(
        self,
        *,
        user_id: uuid.UUID,
        changes: dict[str, object],
        role_names: Sequence[str] | None,
        actor: ActorContext,
    ) -> UserProfile:
        """Apply a partial update; roles are replaced when supplied."""
        await self._authorise_roles(role_names, actor=actor)
        async with self._database.transaction() as session:
            users = UserRepository(session)
            roles = RoleRepository(session)
            sessions = SessionRepository(session)
            audit = AuditService(session)

            user = await users.get_by_id(user_id, for_update=True)
            if user is None:
                raise ResourceNotFoundError("No such user.")

            if user.id == actor.user_id:
                await self._guard_self_edit(
                    users=users, roles=roles, changes=changes, role_names=role_names, actor=actor
                )

            before = {
                "full_name": user.full_name,
                "email": user.email,
                "phone": user.phone,
                "is_active": user.is_active,
                "must_change_password": user.must_change_password,
            }
            if "email" in changes and changes["email"] is not None:
                candidate = str(changes["email"]).lower()
                if candidate != (user.email or "") and await users.email_exists(candidate):
                    raise DuplicateResourceError(
                        "That e-mail address is already in use.",
                        details={"fields": [{"field": "email", "code": "duplicate"}]},
                    )

            deactivated = False
            for field_name in ("full_name", "email", "phone", "is_active", "must_change_password"):
                if field_name in changes:
                    value = changes[field_name]
                    if field_name == "email" and value is not None:
                        value = str(value).lower()
                    setattr(user, field_name, value)
                    if field_name == "is_active" and value is False and before["is_active"] is True:
                        deactivated = True

            if role_names is not None:
                resolved = await self._resolve_roles(roles, role_names, actor=actor)
                await users.assign_roles(user.id, [role.id for role in resolved])

            revoked_sessions = 0
            if deactivated:
                revoked_sessions = await sessions.revoke_all_for_user(
                    user.id, reason=REASON_USER_DEACTIVATED, when=self._now()
                )

            after = {
                "full_name": user.full_name,
                "email": user.email,
                "phone": user.phone,
                "is_active": user.is_active,
                "must_change_password": user.must_change_password,
            }
            audit.record(
                action=AuditAction.USER_DEACTIVATED if deactivated else AuditAction.USER_UPDATED,
                entity_type="user",
                entity_id=user.id,
                old_data=before,
                new_data={**after, "revoked_sessions": revoked_sessions} if deactivated else after,
                actor=actor,
            )
            if role_names is not None:
                audit.record(
                    action=AuditAction.USER_ROLES_CHANGED,
                    entity_type="user",
                    entity_id=user.id,
                    new_data={"roles": sorted(role_names)},
                    actor=actor,
                )
            return await self._profile(session, user)

    async def deactivate_user(self, *, user_id: uuid.UUID, actor: ActorContext) -> UserProfile:
        """Soft delete: deactivate the account and end its sessions (PART 25)."""
        return await self.update_user(
            user_id=user_id, changes={"is_active": False}, role_names=None, actor=actor
        )

    async def set_permission_overrides(
        self,
        *,
        user_id: uuid.UUID,
        overrides: Sequence[PermissionOverride],
        actor: ActorContext,
    ) -> UserProfile:
        """Replace a user's explicit grants/denies (an explicit deny beats a role grant)."""
        # Denies need no authority beyond users.manage; only grants are checked.
        grants = sorted({o.permission_code for o in overrides if o.is_granted})
        denies = sorted({o.permission_code for o in overrides if not o.is_granted})
        await self._authorise_permissions(
            [*grants, *denies], actor=actor, path="user_permission_overrides"
        )
        await self._authorise_permissions(grants, actor=actor, path="user_permission_overrides")
        async with self._database.transaction() as session:
            users = UserRepository(session)
            roles = RoleRepository(session)
            audit = AuditService(session)

            user = await users.get_by_id(user_id, for_update=True)
            if user is None:
                raise ResourceNotFoundError("No such user.")

            catalogue = await roles.permission_codes()
            unknown = sorted({o.permission_code for o in overrides} - catalogue)
            if unknown:
                raise ValidationError(
                    "Unknown permission code.",
                    details={"unknown": unknown, "hint": "Use GET /api/v1/permissions."},
                )

            granted = {o.permission_code for o in overrides if o.is_granted}
            missing = sorted(granted - set(actor.permissions))
            if missing:
                # No escalation: an administrator cannot hand out authority they do not
                # hold themselves, even temporarily. (The pre-check has already audited
                # and refused; this repeats the check inside the transaction so the
                # guarantee does not depend on the caller's ordering.)
                raise PermissionDeniedError(
                    "You cannot grant permissions that you do not hold yourself.",
                    details={"missing_permissions": missing, "reason": "PERMISSION_ESCALATION"},
                )

            before = await users.permission_overrides(user.id)
            await users.set_permission_overrides(
                user.id,
                [
                    {
                        "permission_code": override.permission_code,
                        "is_granted": override.is_granted,
                        "expires_at": override.expires_at,
                        "reason": override.reason,
                        "granted_by": actor.user_id,
                    }
                    for override in overrides
                ],
            )
            audit.record(
                action=AuditAction.USER_PERMISSIONS_CHANGED,
                entity_type="user",
                entity_id=user.id,
                old_data={
                    "overrides": [
                        {
                            "permission_code": row.permission_code,
                            "is_granted": row.is_granted,
                        }
                        for row in before
                    ]
                },
                new_data={
                    "overrides": [
                        {"permission_code": o.permission_code, "is_granted": o.is_granted}
                        for o in overrides
                    ]
                },
                actor=actor,
            )
            return await self._profile(session, user)

    async def replace_role_permissions(
        self,
        *,
        role_id: uuid.UUID,
        permission_codes: Sequence[str],
        actor: ActorContext,
    ) -> Role:
        """Replace a role's permission set, refusing escalation and system roles."""
        await self._authorise_permissions(
            permission_codes,
            actor=actor,
            path="role_permissions",
            detail={"role_id": str(role_id)},
        )

        async with self._database.transaction() as session:
            roles = RoleRepository(session)
            audit = AuditService(session)

            role = await roles.get_role(role_id)
            if role is None:
                raise ResourceNotFoundError("No such role.")
            if role.name in {str(name) for name in SYSTEM_ROLES}:
                raise PermissionDeniedError(
                    f"{role.name} is a system role and cannot be modified.",
                    details={
                        "role": role.name,
                        "system_roles": sorted(str(n) for n in SYSTEM_ROLES),
                    },
                )

            catalogue = await roles.permission_codes()
            unknown = sorted(set(permission_codes) - catalogue)
            if unknown:
                raise ValidationError(
                    "Unknown permission code.",
                    details={"unknown": unknown, "hint": "Use GET /api/v1/permissions."},
                )

            before = await roles.role_permission_codes(role_id)
            missing = sorted(set(permission_codes) - set(actor.permissions))
            if missing:
                # The pre-check above already refused and audited this; repeated here so
                # the transaction itself cannot be talked into an escalation.
                raise PermissionDeniedError(
                    "You cannot grant permissions that you do not hold yourself.",
                    details={"missing_permissions": missing, "reason": "PERMISSION_ESCALATION"},
                )

            await roles.set_role_permissions(role_id, sorted(set(permission_codes)))
            audit.record(
                action=AuditAction.ROLE_PERMISSIONS_CHANGED,
                entity_type="role",
                entity_id=role_id,
                old_data={"permissions": sorted(before)},
                new_data={
                    "permissions": sorted(set(permission_codes)),
                    "added": sorted(set(permission_codes) - before),
                    "removed": sorted(before - set(permission_codes)),
                },
                actor=actor,
            )
            # Members of the role see the change on their next request: the permission
            # fingerprint in their access token no longer matches (app.api.deps).
        return role

    # ------------------------------------------------------------------ helpers
    async def _guard_self_edit(
        self,
        *,
        users: UserRepository,
        roles: RoleRepository,
        changes: dict[str, object],
        role_names: Sequence[str] | None,
        actor: ActorContext,
    ) -> None:
        """Refuse the two self-edits that would lock the caller out of administration."""
        if changes.get("is_active") is False:
            raise ValidationError(
                "You cannot deactivate your own account.",
                details={"fields": [{"field": "is_active", "code": "self_deactivation"}]},
            )
        if role_names is None:
            return
        if PermissionCode.USERS_MANAGE.value not in set(actor.permissions):
            return

        # Would the new role set still grant user management? Explicit overrides count,
        # because they survive a role change.
        candidate_roles = await users.roles_by_names(list(role_names))
        still_granted = PermissionCode.USERS_MANAGE.value in {
            code for role in candidate_roles for code in await roles.role_permission_codes(role.id)
        }
        overrides = await users.permission_overrides(actor.user_id) if actor.user_id else []
        override_grants = {
            override.permission_code
            for override in overrides
            if override.is_granted
            and (override.expires_at is None or override.expires_at > self._now())
        }
        if not still_granted and PermissionCode.USERS_MANAGE.value not in override_grants:
            raise ValidationError(
                "You cannot remove your own user-management authority.",
                details={"fields": [{"field": "roles", "code": "self_lockout"}]},
            )

    async def _resolve_roles(
        self,
        roles: RoleRepository,
        role_names: Sequence[str],
        *,
        actor: ActorContext,
    ) -> list[Role]:
        """Resolve role names, refusing escalation to a role richer than the actor's.

        Two independent guards run here, because a role is not only a bundle of
        permissions:

        * **permission escalation** — an administrator may not assign a role that grants
          authority they do not hold themselves;
        * **system-role escalation** — ``SUPER_ADMIN`` is a system role (see
          ``SECURITY.md`` §4). Holding every permission today does not make it assignable:
          only someone who *is* a super administrator may create another one.
        """
        if not role_names:
            return []
        catalogue = await roles.list_roles()
        by_name = {role.name.upper(): role for role in catalogue}
        unknown = sorted({name.upper() for name in role_names} - set(by_name))
        if unknown:
            raise ValidationError(
                "Unknown role.",
                details={"unknown": unknown, "known": sorted(by_name)},
            )

        resolved = [by_name[name.upper()] for name in role_names]
        actor_permissions = set(actor.permissions)
        actor_roles = {name.upper() for name in actor.roles}
        for role in resolved:
            if is_system_role(role.name) and role.name.upper() not in actor_roles:
                raise PermissionDeniedError(
                    f"You cannot assign the system role {role.name}.",
                    details={
                        "role": role.name,
                        "reason": "SYSTEM_ROLE_ESCALATION",
                        "path": "system_role_assignment",
                    },
                )
            granted = await roles.role_permission_codes(role.id)
            missing = sorted(granted - actor_permissions)
            if missing:
                raise PermissionDeniedError(
                    f"You cannot assign the role {role.name}: "
                    "it grants permissions you do not hold.",
                    details={
                        "role": role.name,
                        "missing_permissions": missing,
                        "reason": "PERMISSION_ESCALATION",
                        "path": "role_assignment",
                    },
                )
        return resolved


def role_names_of(role_rows: Sequence[Role]) -> list[str]:
    """Convenience for callers that hold role rows rather than a profile."""
    return sorted(role.name for role in role_rows)


def is_system_role(name: str) -> bool:
    """True for roles the operator may not edit (SUPER_ADMIN)."""
    return name in {str(role) for role in SYSTEM_ROLES}


def default_role_names() -> list[str]:
    """Role names the API documents for a first administrator (used by bootstrap docs)."""
    return [str(RoleName.SUPER_ADMIN)]


def build_user_service(*, database: Database, settings: Settings) -> UserService:
    """Construct the service with an Argon2id hasher built from validated settings.

    The hasher is only used to create new credentials here (verification happens in
    :mod:`app.services.auth_service`), so it is built with the configured parameters.
    """
    from app.core.security import build_password_hasher

    hasher = build_password_hasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost,
        parallelism=settings.argon2_parallelism,
    )
    return UserService(database=database, settings=settings, hasher=hasher)


__all__ = [
    "CreatedUser",
    "PermissionOverride",
    "UserProfile",
    "UserService",
    "build_user_service",
    "default_role_names",
    "is_system_role",
    "role_names_of",
]
