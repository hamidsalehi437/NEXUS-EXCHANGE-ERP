"""Scheduled maintenance tasks (PART 18, PART 25, PART 40).

Both tasks are bounded and idempotent: they process a limited batch per run, stop when
there is nothing left to do, and can be re-run at any time without changing the outcome.
Neither task touches the ledger or the audit log — financial and audit history is
retained forever (PART 22); only operationally-expired rows are removed.
"""

from __future__ import annotations

from typing import Any

from celery import Task
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError

from app.core.config import get_settings
from app.core.database import create_sync_engine
from app.core.logging import get_logger
from app.worker.celery_app import celery_app

logger = get_logger(__name__)

# Rows handled per statement. Keeps each transaction short so maintenance never
# holds locks for long on a busy production database.
_BATCH_SIZE = 5_000

_SWEEP_EXPIRED_REFRESH_TOKENS = text(
    """
    WITH expired AS (
        SELECT id
        FROM refresh_tokens
        WHERE revoked_at IS NULL
          AND expires_at < NOW()
        ORDER BY expires_at
        LIMIT :batch_size
    )
    UPDATE refresh_tokens t
       SET revoked_at = NOW(),
           revoked_reason = 'EXPIRED'
      FROM expired
     WHERE t.id = expired.id
    RETURNING t.id
    """
)

_PRUNE_IDEMPOTENCY_KEYS = text(
    """
    WITH expired AS (
        SELECT id
        FROM idempotency_keys
        WHERE status IN ('COMPLETED', 'FAILED')
          AND created_at < NOW() - make_interval(days => :retention_days)
        ORDER BY created_at
        LIMIT :batch_size
    )
    DELETE FROM idempotency_keys k
     USING expired
     WHERE k.id = expired.id
    RETURNING k.id
    """
)


class _MaintenanceTask(Task):  # type: ignore[misc]
    """Base task owning one lazily created engine per worker process."""

    _engine: Any = None

    def engine(self) -> Any:
        if self._engine is None:
            self._engine = create_sync_engine(get_settings(), purpose="worker")
        return self._engine


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    base=_MaintenanceTask,
    name="app.worker.maintenance.sweep_expired_refresh_tokens",
    autoretry_for=(OperationalError, InterfaceError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=5,
)
def sweep_expired_refresh_tokens(self: _MaintenanceTask) -> dict[str, Any]:
    """Revoke refresh tokens that have passed their expiry.

    Rotation already invalidates a token when it is used (PART 42); this sweep closes
    the window for tokens that were simply never used again, so the set of tokens that
    could still be presented shrinks to the ones that are genuinely live.
    """
    revoked = 0
    while True:
        # One transaction per batch: a long sweep must not hold row locks or an open
        # transaction for the whole run.
        with self.engine().begin() as connection:
            batch = (
                connection.execute(_SWEEP_EXPIRED_REFRESH_TOKENS, {"batch_size": _BATCH_SIZE})
                .scalars()
                .all()
            )
        revoked += len(batch)
        if len(batch) < _BATCH_SIZE:
            break

    result = {"task": "sweep_expired_refresh_tokens", "revoked": revoked}
    logger.info("maintenance_completed", **result)
    return result


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    base=_MaintenanceTask,
    name="app.worker.maintenance.prune_idempotency_keys",
    autoretry_for=(OperationalError, InterfaceError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=5,
)
def prune_idempotency_keys(self: _MaintenanceTask) -> dict[str, Any]:
    """Delete completed idempotency records older than the retention window.

    Only COMPLETED and FAILED records are removed, and only after
    ``IDEMPOTENCY_RETENTION_DAYS``. A retry from a device that has been offline longer
    than the window is treated as a new request, which is the documented behaviour in
    ``docs/api/API_CONTRACT.md``. Records still IN_PROGRESS are never touched: the
    request they belong to may still be running.
    """
    settings = get_settings()
    deleted = 0
    while True:
        with self.engine().begin() as connection:
            batch = (
                connection.execute(
                    _PRUNE_IDEMPOTENCY_KEYS,
                    {
                        "retention_days": settings.idempotency_retention_days,
                        "batch_size": _BATCH_SIZE,
                    },
                )
                .scalars()
                .all()
            )
        deleted += len(batch)
        if len(batch) < _BATCH_SIZE:
            break

    result = {
        "task": "prune_idempotency_keys",
        "deleted": deleted,
        "retention_days": settings.idempotency_retention_days,
    }
    logger.info("maintenance_completed", **result)
    return result


__all__ = ["prune_idempotency_keys", "sweep_expired_refresh_tokens"]
