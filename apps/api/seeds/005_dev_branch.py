"""Bootstrap branch for development and test environments (PART 7, PART 45).

Devices, sessions and (from Phase 5) transactions all hang off a branch, so an
environment without one cannot be signed into: the first login would have nowhere to
register the counter. This seed therefore creates a single ``MAIN`` branch **only** in
the same guarded situation as the development administrator — ``APP_ENV=development``
plus a ``DEV_ADMIN_PASSWORD`` — and never in production, where branches are created by
an administrator through the API with an audit trail.

It is idempotent in the strongest sense: it only acts when the ``branches`` table is
completely empty, so an operator's own branches are never touched, renamed or joined by
a surprise second one.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.branch import Branch
from seeds.base import SeedContext, SeedCounts

DEFAULT_BRANCH_CODE = "MAIN"
DEFAULT_BRANCH_NAME = "Main Branch"


def run(context: SeedContext) -> SeedCounts:
    """Create the bootstrap branch when the guarded environment is empty."""
    settings = context.settings
    counts = SeedCounts()

    if settings.app_env != "development":
        context.record_audit(
            action="SEED_DEV_BRANCH_SKIPPED",
            entity_type="branch",
            details={"reason": "app_env is not development"},
        )
        return counts

    if not settings.dev_admin_password:
        context.record_audit(
            action="SEED_DEV_BRANCH_SKIPPED",
            entity_type="branch",
            details={"reason": "DEV_ADMIN_PASSWORD is empty"},
        )
        return counts

    existing = context.session.execute(select(Branch).limit(1)).scalar_one_or_none()
    if existing is not None:
        # Any branch at all means the operator has taken over this concern.
        counts.unchanged += 1
        context.counts.merge(counts)
        return counts

    branch = Branch(
        code=DEFAULT_BRANCH_CODE,
        name=DEFAULT_BRANCH_NAME,
        timezone="Asia/Kabul",
        is_active=True,
    )
    context.session.add(branch)
    counts.inserted += 1

    context.record_audit(
        action="SEED_DEV_BRANCH_CREATED",
        entity_type="branch",
        details={
            "code": DEFAULT_BRANCH_CODE,
            "name": DEFAULT_BRANCH_NAME,
            "environment": settings.app_env,
            "note": "bootstrap branch so a first device can register",
        },
    )
    context.counts.merge(counts)
    return counts


__all__ = ["DEFAULT_BRANCH_CODE", "DEFAULT_BRANCH_NAME", "run"]
