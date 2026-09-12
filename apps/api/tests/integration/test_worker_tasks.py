"""Worker tasks against a real database.

These tasks are the system's own self-check (ledger and audit integrity) and its
operational housekeeping (expired refresh tokens, idempotency retention). Each task is
executed for real against a migrated, seeded database: the SQL is the behaviour under
test, so none of it is mocked.

The broker is not involved. ``celery_app.task_always_eager`` plus ``Task.apply()`` runs
the registered task function inline, which is exactly what the worker process executes.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.sql import text

# Importing the modules registers their tasks, mirroring ``include=[...]`` in
# celery_app, which is what a real worker process does at startup.
import app.worker.integrity
import app.worker.maintenance  # noqa: F401  (imported to register its tasks)
from app.core.config import get_settings
from app.core.database import create_sync_engine
from app.worker.celery_app import celery_app
from tests.helpers import (
    DEV_ADMIN_PASSWORD,
    create_database,
    database_dsn,
    drop_database,
    migrate,
    run_seeds,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_WORKER_DATABASE = "nexus_test_worker"
_AUDIT_CHAIN_TASK = "app.worker.integrity.verify_audit_chain"
_LEDGER_TASK = "app.worker.integrity.verify_ledger_integrity"
_SWEEP_TASK = "app.worker.maintenance.sweep_expired_refresh_tokens"
_PRUNE_TASK = "app.worker.maintenance.prune_idempotency_keys"


def _hash(seed: str) -> str:
    """A 64-character hex value for the CHAR(64) hash columns (no crypto meaning)."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _run_task(name: str, engine: Engine) -> dict[str, object]:
    """Run a registered task inline against ``engine``."""
    task = celery_app.tasks[name]
    previous = getattr(task, "_engine", None)
    task._engine = engine
    try:
        return task.apply().get()  # type: ignore[no-any-return]
    finally:
        task._engine = previous


@pytest.fixture(scope="module")
def worker_engine() -> Iterator[Engine]:
    """A migrated, seeded disposable database, seeded as development (dev admin exists).

    The task classes build their engine lazily from settings on first use; injecting an
    engine here keeps the tasks pointed at a disposable database instead of the
    developer's ``nexus_exchange``.
    """
    create_database(_WORKER_DATABASE)
    migrate(_WORKER_DATABASE)
    seeded = run_seeds(
        _WORKER_DATABASE,
        extra_env={"APP_ENV": "development", "DEV_ADMIN_PASSWORD": DEV_ADMIN_PASSWORD},
    )
    assert seeded.returncode == 0, seeded.stderr

    engine = create_engine(database_dsn(_WORKER_DATABASE), future=True)
    try:
        yield engine
    finally:
        engine.dispose()
        drop_database(_WORKER_DATABASE)


class TestIntegrityTasks:
    def test_ledger_integrity_passes_on_the_seeded_database(self, worker_engine: Engine) -> None:
        result = _run_task(_LEDGER_TASK, worker_engine)

        assert result["ok"] is True, result
        assert result["task"] == "verify_ledger_integrity"
        assert result["total_debit"] == result["total_credit"]
        assert result["unbalanced_entries"] == 0
        assert result["balance_cache_mismatches"] == 0

    def test_audit_chain_passes_on_the_seeded_database(self, worker_engine: Engine) -> None:
        result = _run_task(_AUDIT_CHAIN_TASK, worker_engine)

        assert result["ok"] is True, result
        assert result["task"] == "verify_audit_chain"
        assert result["breaks"] == 0
        # The seeds write audit rows (seed runs are recorded), so the chain is non-empty.
        assert result["audit_rows"] >= 1

    def test_integrity_tasks_are_read_only(self, worker_engine: Engine) -> None:
        """A verification run must not change the ledger or the audit log."""
        with worker_engine.connect() as connection:
            before_lines = connection.execute(
                text("SELECT COUNT(*) FROM journal_lines")
            ).scalar_one()
            before_audit = connection.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar_one()

        _run_task(_LEDGER_TASK, worker_engine)
        _run_task(_AUDIT_CHAIN_TASK, worker_engine)

        with worker_engine.connect() as connection:
            after_lines = connection.execute(
                text("SELECT COUNT(*) FROM journal_lines")
            ).scalar_one()
            after_audit = connection.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar_one()

        assert (after_lines, after_audit) == (before_lines, before_audit)

    def test_a_tampered_audit_row_is_reported_not_hidden(self, worker_engine: Engine) -> None:
        """I-6: editing an audit row must make the chain check fail loudly.

        Disabling the append-only triggers is what an attacker with database access
        would do — exactly the situation the tamper-evident chain exists to expose. This
        runs last in the class so the corruption cannot affect the passing checks above.
        """
        try:
            with worker_engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO audit_logs (action, entity_type, new_data, request_id)
                        VALUES ('WORKER_PROBE', 'worker-test',
                                '{"value": 1}'::jsonb, 'worker-tamper')
                        """
                    )
                )
                connection.execute(text("ALTER TABLE audit_logs DISABLE TRIGGER USER"))
                connection.execute(
                    text(
                        """
                        UPDATE audit_logs
                           SET new_data = '{"value": 2}'::jsonb
                         WHERE request_id = 'worker-tamper'
                        """
                    )
                )

            result = _run_task(_AUDIT_CHAIN_TASK, worker_engine)
        finally:
            with worker_engine.begin() as connection:
                connection.execute(text("ALTER TABLE audit_logs ENABLE TRIGGER USER"))

        assert result["ok"] is False
        assert result["breaks"] >= 1
        assert result["breaks_detail"], "a break must be reported with the offending row"
        assert result["audit_rows"] >= 1


class TestRefreshTokenSweep:
    def _insert_token(
        self,
        engine: Engine,
        *,
        suffix: str,
        issued_days_ago: int,
        expires_in_days: int,
    ) -> uuid.UUID:
        """Insert a refresh token for the dev admin.

        ``ck_refresh_tokens_expiry`` requires ``expires_at > issued_at``; a negative
        ``expires_in_days`` places the expiry in the past while keeping it after
        ``issued_at``.
        """
        token_id = uuid.uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO refresh_tokens (
                        id, user_id, family_id, token_hash, issued_at, expires_at
                    )
                    SELECT :id, u.id, :family_id, :token_hash,
                           NOW() - make_interval(days => :issued_days_ago),
                           NOW() + make_interval(days => :expires_in_days)
                    FROM users u
                    WHERE u.username = 'admin'
                    """
                ),
                {
                    "id": token_id,
                    "family_id": uuid.uuid4(),
                    "token_hash": _hash(suffix),
                    "issued_days_ago": issued_days_ago,
                    "expires_in_days": expires_in_days,
                },
            )
        return token_id

    def _row(self, engine: Engine, token_id: uuid.UUID) -> tuple[object, object]:
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT revoked_at, revoked_reason FROM refresh_tokens WHERE id = :id"),
                {"id": token_id},
            ).one()
        return row[0], row[1]

    def test_expired_tokens_are_revoked_and_live_tokens_are_untouched(
        self, worker_engine: Engine
    ) -> None:
        expired_id = self._insert_token(
            worker_engine, suffix="expired", issued_days_ago=30, expires_in_days=-1
        )
        live_id = self._insert_token(
            worker_engine, suffix="live", issued_days_ago=0, expires_in_days=7
        )
        already_revoked_id = self._insert_token(
            worker_engine, suffix="revoked", issued_days_ago=30, expires_in_days=-1
        )
        with worker_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE refresh_tokens
                       SET revoked_at = NOW() - make_interval(days => 2),
                           revoked_reason = 'ROTATED'
                     WHERE id = :id
                    """
                ),
                {"id": already_revoked_id},
            )

        result = _run_task(_SWEEP_TASK, worker_engine)

        assert result["task"] == "sweep_expired_refresh_tokens"
        assert result["revoked"] >= 1

        revoked_at, revoked_reason = self._row(worker_engine, expired_id)
        assert revoked_at is not None, "an expired token must be revoked"
        assert revoked_reason == "EXPIRED"

        live_revoked_at, live_reason = self._row(worker_engine, live_id)
        assert live_revoked_at is None, "a live token must not be revoked"
        assert live_reason is None

        _, existing_reason = self._row(worker_engine, already_revoked_id)
        assert existing_reason == "ROTATED", "an existing revocation reason must be preserved"

    def test_the_sweep_is_idempotent(self, worker_engine: Engine) -> None:
        first = _run_task(_SWEEP_TASK, worker_engine)
        second = _run_task(_SWEEP_TASK, worker_engine)

        assert first["revoked"] >= 0
        assert second["revoked"] == 0, "a second sweep must find nothing left to revoke"


class TestIdempotencyRetention:
    def _insert_key(self, engine: Engine, *, status: str, age_days: int, suffix: str) -> uuid.UUID:
        key_id = uuid.uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO idempotency_keys (
                        id, key, user_id, endpoint, request_hash, status,
                        response_status, created_at, completed_at
                    )
                    SELECT :id, :key, u.id, '/api/v1/exchange', :request_hash, :status, 200,
                           NOW() - make_interval(days => :age),
                           NOW() - make_interval(days => :age)
                    FROM users u
                    WHERE u.username = 'admin'
                    """
                ),
                {
                    "id": key_id,
                    "key": uuid.uuid4(),
                    "request_hash": _hash(suffix),
                    "status": status,
                    "age": age_days,
                },
            )
        return key_id

    def _remaining(self, engine: Engine, ids: list[uuid.UUID]) -> set[uuid.UUID]:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT id FROM idempotency_keys WHERE id = ANY(:ids)"), {"ids": ids}
            ).all()
        return {row[0] for row in rows}

    def test_expired_records_are_removed_and_others_are_kept(self, worker_engine: Engine) -> None:
        retention = get_settings().idempotency_retention_days
        old_completed = self._insert_key(
            worker_engine, status="COMPLETED", age_days=retention + 5, suffix="old-completed"
        )
        old_failed = self._insert_key(
            worker_engine, status="FAILED", age_days=retention + 5, suffix="old-failed"
        )
        fresh_completed = self._insert_key(
            worker_engine, status="COMPLETED", age_days=1, suffix="fresh-completed"
        )
        old_in_progress = self._insert_key(
            worker_engine, status="IN_PROGRESS", age_days=retention + 5, suffix="old-in-progress"
        )

        result = _run_task(_PRUNE_TASK, worker_engine)

        assert result["task"] == "prune_idempotency_keys"
        assert result["retention_days"] == retention
        assert result["deleted"] >= 2

        remaining = self._remaining(
            worker_engine, [old_completed, old_failed, fresh_completed, old_in_progress]
        )
        assert old_completed not in remaining, "an expired COMPLETED record must be pruned"
        assert old_failed not in remaining, "an expired FAILED record must be pruned"
        assert fresh_completed in remaining, "a record inside the retention window is kept"
        assert old_in_progress in remaining, "IN_PROGRESS records are never pruned"

    def test_pruning_is_idempotent(self, worker_engine: Engine) -> None:
        _run_task(_PRUNE_TASK, worker_engine)
        assert _run_task(_PRUNE_TASK, worker_engine)["deleted"] == 0


class TestWorkerRegistration:
    def test_every_scheduled_task_is_registered(self) -> None:
        for entry in celery_app.conf.beat_schedule.values():
            assert entry["task"] in celery_app.tasks, entry["task"]

    def test_maintenance_tasks_are_routed_to_the_maintenance_queue(self) -> None:
        routes = celery_app.conf.task_routes
        assert routes["app.worker.maintenance.*"]["queue"] == "maintenance"
        assert routes["app.worker.integrity.*"]["queue"] == "integrity"

    def test_the_worker_engine_is_a_sync_psycopg_engine(self) -> None:
        engine = create_sync_engine(get_settings(), purpose="test-worker")
        try:
            assert engine.dialect.name == "postgresql"
            assert engine.dialect.driver == "psycopg"
        finally:
            engine.dispose()
