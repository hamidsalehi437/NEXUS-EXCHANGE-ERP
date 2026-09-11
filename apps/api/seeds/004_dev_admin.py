"""Development-only administrator seed (PART 45).

The master prompt is explicit: a default administrator exists **only** in
development; in production the first administrator is created through the
documented bootstrap procedure, never by a seed that ships a known password.

Guards enforced here:

* refuses to run unless ``APP_ENV=development``;
* refuses to run when ``DEV_ADMIN_PASSWORD`` is empty;
* enforces the password policy before hashing;
* hashes with the configured Argon2id parameters — the plaintext is never logged,
  never stored and never echoed by the CLI;
* is idempotent: an existing administrator is left untouched (its password is
  never silently reset by a re-seed).
"""

from __future__ import annotations

from sqlalchemy import select

from app.core.permissions import RoleName
from app.core.security import PasswordPolicyError, build_password_hasher, enforce_password_policy
from app.models.role import Role
from app.models.user import User, UserRole
from seeds.base import SeedContext, SeedCounts


def run(context: SeedContext) -> SeedCounts:
    """Create the development administrator when the guards allow it."""
    settings = context.settings
    counts = SeedCounts()

    if settings.app_env != "development":
        context.record_audit(
            action="SEED_DEV_ADMIN_SKIPPED",
            entity_type="user",
            details={"reason": "app_env is not development"},
        )
        return counts

    if not settings.dev_admin_password:
        context.record_audit(
            action="SEED_DEV_ADMIN_SKIPPED",
            entity_type="user",
            details={"reason": "DEV_ADMIN_PASSWORD is empty"},
        )
        return counts

    username = settings.dev_admin_username.strip().lower()
    existing = context.session.execute(
        select(User).where(User.username == username)
    ).scalar_one_or_none()

    if existing is not None:
        counts.unchanged += 1
        context.counts.merge(counts)
        return counts

    try:
        enforce_password_policy(
            settings.dev_admin_password,
            username=username,
            full_name=settings.dev_admin_full_name,
        )
    except PasswordPolicyError as exc:
        raise SystemExit(f"DEV_ADMIN_PASSWORD rejected by the password policy: {exc}") from exc

    hasher = build_password_hasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost,
        parallelism=settings.argon2_parallelism,
    )

    admin = User(
        username=username,
        full_name=settings.dev_admin_full_name,
        password_hash=hasher.hash(settings.dev_admin_password),
        is_active=True,
        must_change_password=False,
    )
    context.session.add(admin)
    context.session.flush()

    super_admin = context.session.execute(
        select(Role).where(Role.name == str(RoleName.SUPER_ADMIN))
    ).scalar_one_or_none()
    if super_admin is None:
        raise RuntimeError("SUPER_ADMIN role is missing: run the roles seed first")

    context.session.add(UserRole(user_id=admin.id, role_id=super_admin.id))
    counts.inserted += 2  # user + role assignment

    context.record_audit(
        action="SEED_DEV_ADMIN_CREATED",
        entity_type="user",
        details={
            "username": username,
            "role": str(RoleName.SUPER_ADMIN),
            "environment": settings.app_env,
            "password_policy": "enforced",
            "argon2id": hasher.parameters,
        },
    )

    context.counts.merge(counts)
    return counts
