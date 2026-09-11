"""Celery application for the NEXUS worker (PART 1, PART 44).

Design rules encoded here:

* **UTC everywhere** — ``timezone="UTC"`` and ``enable_utc=True``; a task must never
  reason in a local zone (PART 5).
* **No silent loss** — ``task_acks_late`` plus ``task_reject_on_worker_lost`` means an
  integrity task that dies mid-run is redelivered instead of being dropped.
* **One task at a time per process** — ``worker_prefetch_multiplier=1`` keeps a long
  verification from blocking a queue behind it, and ``task_time_limit`` bounds a run.
* **Queues** — verification work goes to ``integrity``, so a future high-volume queue
  (sync, reports, backups) cannot starve it.
* **Eager mode for tests** — ``NEXUS_CELERY_TASK_ALWAYS_EAGER`` runs tasks inline so the
  test suite exercises the real task code without a broker.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

settings = get_settings()
configure_logging(level=settings.log_level, fmt=settings.log_format)
logger = get_logger(__name__)

# Verification runs on `integrity`, scheduled upkeep on `maintenance`; keeping them
# apart means a long verification cannot delay token/idempotency housekeeping.
DEFAULT_QUEUE = "maintenance"
WORKER_QUEUES = ("integrity", "maintenance")

# Importing the task modules registers them on the app. They are imported at the
# bottom so the app object exists before the modules import it back.
celery_app = Celery(
    "nexus_exchange",
    broker=settings.broker_url,
    backend=settings.result_backend_url,
    include=["app.worker.integrity", "app.worker.maintenance"],
)

celery_app.conf.update(
    timezone="UTC",
    enable_utc=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Never lose work: acknowledge after the task returns, and requeue if the child
    # process is killed (OOM, deploy) instead of leaving the message unacked.
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=200,
    task_time_limit=settings.worker_task_time_limit_seconds,
    task_soft_time_limit=settings.worker_task_soft_time_limit_seconds,
    result_expires=settings.worker_result_expires_seconds,
    task_default_queue=DEFAULT_QUEUE,
    task_queues={
        "integrity": {"exchange": "integrity", "routing_key": "integrity"},
        "maintenance": {"exchange": "maintenance", "routing_key": "maintenance"},
    },
    worker_direct=True,
    task_routes={
        "app.worker.integrity.*": {"queue": "integrity"},
        "app.worker.maintenance.*": {"queue": "maintenance"},
    },
    beat_schedule={
        # Tamper-evidence check: confirms the audit hash chain is still intact.
        "verify-audit-chain": {
            "task": "app.worker.integrity.verify_audit_chain",
            "schedule": crontab(minute=f"*/{settings.audit_chain_check_minutes}"),
            "options": {"queue": "integrity", "expires": 900},
        },
        # Revoke refresh tokens that expired without being rotated (PART 42).
        "sweep-expired-refresh-tokens": {
            "task": "app.worker.maintenance.sweep_expired_refresh_tokens",
            "schedule": crontab(minute=settings.token_sweep_minute_of_hour),
            "options": {"queue": DEFAULT_QUEUE, "expires": 1800},
        },
        # Retention for idempotency records (PART 40): completed answers age out
        # after IDEMPOTENCY_RETENTION_DAYS; in-progress records are never touched.
        "prune-idempotency-keys": {
            "task": "app.worker.maintenance.prune_idempotency_keys",
            "schedule": crontab(
                minute=settings.idempotency_prune_minute_of_hour,
                hour=settings.idempotency_prune_hour,
            ),
            "options": {"queue": DEFAULT_QUEUE, "expires": 3600},
        },
        # Ledger self-check: debits == credits and the balance cache matches the
        # journal, i.e. PART 49 invariants, on a running production database.
        "verify-ledger-integrity": {
            "task": "app.worker.integrity.verify_ledger_integrity",
            "schedule": crontab(
                minute=settings.ledger_check_minute_of_hour,
                hour=f"*/{settings.ledger_check_every_hours}",
            ),
            "options": {"queue": "integrity", "expires": 3600},
        },
    },
)


@celery_app.on_after_configure.connect  # type: ignore[untyped-decorator]
def _log_configuration(_sender: Celery | None = None, **_kwargs: object) -> None:
    """One line at startup so operators can see which broker/queues are in use."""
    logger.info(
        "worker_configured",
        broker=_redacted(settings.broker_url),
        backend=_redacted(settings.result_backend_url),
        eager=settings.celery_task_always_eager,
        queues=list(WORKER_QUEUES),
    )


def _redacted(url: str) -> str:
    """Strip credentials from a broker URL before it reaches a log line."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    return f"{scheme}://***@{rest.rsplit('@', 1)[-1]}"


__all__ = ["celery_app"]
