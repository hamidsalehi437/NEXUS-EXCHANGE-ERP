"""Background worker package (Celery).

The worker runs only *verification and maintenance* work in Phase 1. Financial
writes stay inside the API request transaction (PART 47) so a task can never be
half-applied to the ledger: the tasks registered here read the ledger and report,
they never post to it.
"""

from app.worker.celery_app import celery_app

__all__ = ["celery_app"]
