"""Read-only integrity verification tasks (PART 49, PART 18).

These tasks exist so the two invariants the whole system rests on are checked on a
**running** database, not only in a test suite:

1. ``verify_audit_chain`` — the tamper-evident audit hash chain is still intact. The
   database computes this in ``verify_audit_chain()`` (docs/database/schema.sql); the
   task calls the database's own function rather than re-implementing the hashing in
   Python, so there is exactly one definition of "intact".
2. ``verify_ledger_integrity`` — total debits equal total credits, every journal
   entry is balanced on its own, and the ``account_balances`` cache equals the sum of
   the journal movements it is derived from (the Phase 0 I-3 invariant).

Neither task writes to the ledger. A detected failure is reported as a task result
with ``ok = False`` plus an ``integrity_violation`` log event: it is *not* retried,
because retrying cannot repair tampering, and it is *not* raised as an exception in a
way that would hide the numbers an operator needs to see.
"""

from __future__ import annotations

from typing import Any

from celery import Task
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError

from app.core.database import create_sync_engine
from app.core.logging import get_logger
from app.worker.celery_app import celery_app

logger = get_logger(__name__)

# Break counts beyond this are summarised instead of returned row by row.
_MAX_REPORTED_ROWS = 50

# The balance cache is maintained by the nexus_maintain_account_balances trigger per
# (account_id, currency_id). Journal entries are immutable and every stored entry is a
# posted entry, so no status filter exists or is needed.
_BALANCE_CACHE_MISMATCHES = text(
    """
    WITH journal AS (
        SELECT account_id,
               currency_id,
               SUM(debit)  AS debit_total,
               SUM(credit) AS credit_total
        FROM journal_lines
        GROUP BY account_id, currency_id
    )
    SELECT a.code                       AS account_code,
           c.code                       AS currency_code,
           COALESCE(b.debit_total, 0)   AS cached_debit,
           COALESCE(b.credit_total, 0)  AS cached_credit,
           COALESCE(j.debit_total, 0)   AS journal_debit,
           COALESCE(j.credit_total, 0)  AS journal_credit
    FROM accounts a
    CROSS JOIN currencies c
    LEFT JOIN account_balances b ON b.account_id = a.id AND b.currency_id = c.id
    LEFT JOIN journal j          ON j.account_id = a.id AND j.currency_id = c.id
    WHERE COALESCE(b.debit_total, 0)  <> COALESCE(j.debit_total, 0)
       OR COALESCE(b.credit_total, 0) <> COALESCE(j.credit_total, 0)
    ORDER BY a.code, c.code
    """
)

_LEDGER_TOTALS = text(
    """
    SELECT COALESCE(SUM(debit), 0)  AS total_debit,
           COALESCE(SUM(credit), 0) AS total_credit
    FROM journal_lines
    """
)

# Balance is enforced per entry on write; this re-checks it from the stored rows.
_UNBALANCED_ENTRIES = text(
    """
    SELECT e.id::text AS journal_entry_id,
           e.reference_type,
           e.transaction_date,
           SUM(l.debit)  AS total_debit,
           SUM(l.credit) AS total_credit
    FROM journal_entries e
    JOIN journal_lines l ON l.journal_entry_id = e.id
    GROUP BY e.id, e.reference_type, e.transaction_date
    HAVING SUM(l.debit) <> SUM(l.credit)
    ORDER BY e.transaction_date
    """
)

_BROKEN_AUDIT_LINKS = text("SELECT * FROM verify_audit_chain()")


class _DatabaseTask(Task):  # type: ignore[misc]
    """Base task that owns one lazily created engine per worker process.

    The engine is created on first use inside the worker process (never at import
    time), so importing this module — for example during ``celery inspect`` — does not
    require a reachable database.
    """

    _engine: Any = None

    def engine(self) -> Any:
        if self._engine is None:
            from app.core.config import get_settings

            self._engine = create_sync_engine(get_settings(), purpose="worker")
        return self._engine


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    base=_DatabaseTask,
    name="app.worker.integrity.verify_audit_chain",
    autoretry_for=(OperationalError, InterfaceError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=5,
)
def verify_audit_chain(self: _DatabaseTask) -> dict[str, Any]:
    """Verify the audit hash chain and report any break.

    A connection failure (:class:`~sqlalchemy.exc.OperationalError`) is retried with
    backoff — that is an infrastructure problem, not a finding. A detected break is
    returned, never retried, and a query error surfaces immediately instead of being
    masked by retries.
    """
    with self.engine().connect() as connection:
        broken = connection.execute(_BROKEN_AUDIT_LINKS).mappings().all()
        checked = connection.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar_one()

    result: dict[str, Any] = {
        "task": "verify_audit_chain",
        "ok": not broken,
        "audit_rows": int(checked),
        "breaks": len(broken),
        "breaks_detail": [dict(row) for row in broken[:_MAX_REPORTED_ROWS]],
    }
    if broken:
        logger.error("integrity_violation", check="audit_chain", **result)
    else:
        logger.info("integrity_check_passed", check="audit_chain", audit_rows=result["audit_rows"])
    return result


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    base=_DatabaseTask,
    name="app.worker.integrity.verify_ledger_integrity",
    autoretry_for=(OperationalError, InterfaceError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=5,
)
def verify_ledger_integrity(self: _DatabaseTask) -> dict[str, Any]:
    """Check debits == credits, per-entry balance, and the balance cache."""
    with self.engine().connect() as connection:
        totals = connection.execute(_LEDGER_TOTALS).mappings().one()
        unbalanced = connection.execute(_UNBALANCED_ENTRIES).mappings().all()
        mismatches = connection.execute(_BALANCE_CACHE_MISMATCHES).mappings().all()

    total_debit = totals["total_debit"]
    total_credit = totals["total_credit"]
    balanced = total_debit == total_credit
    ok = balanced and not unbalanced and not mismatches

    result: dict[str, Any] = {
        "task": "verify_ledger_integrity",
        "ok": ok,
        "total_debit": str(total_debit),
        "total_credit": str(total_credit),
        "difference": str(total_debit - total_credit),
        "unbalanced_entries": len(unbalanced),
        "balance_cache_mismatches": len(mismatches),
        "unbalanced_detail": [dict(row) for row in unbalanced[:_MAX_REPORTED_ROWS]],
        "mismatch_detail": [
            {key: str(value) for key, value in dict(row).items()}
            for row in mismatches[:_MAX_REPORTED_ROWS]
        ],
    }
    if ok:
        logger.info("integrity_check_passed", check="ledger", total_debit=str(total_debit))
    else:
        logger.error("integrity_violation", check="ledger", **result)
    return result


__all__ = ["verify_audit_chain", "verify_ledger_integrity"]
