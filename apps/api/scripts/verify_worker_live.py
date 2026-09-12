"""Prove that the Celery tasks really travel over the broker and result backend.

``tests/integration/test_worker_tasks.py`` exercises the task bodies directly
against the database, which proves the *logic* but not the wiring.  This script
is the transport proof for operators: it publishes each task with
``send_task`` (so the API-side Celery app, the Redis broker and the Redis result
backend are all involved), waits for a worker to consume it, and checks that the
maintenance tasks actually acted on rows seeded for the purpose.

Usage (from ``apps/api``, with a worker running on the integrity+maintenance
queues)::

    PYTHONPATH=. python scripts/verify_worker_live.py

Exit code 0 means every task reported SUCCESS and both maintenance tasks were
observed to have changed the database.  The probe rows are set up and cleaned up
by the script itself, using an append-only-safe cleanup (deactivation, never a
DELETE) because the database forbids deleting historical rows.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid
from datetime import UTC, datetime

from celery.result import AsyncResult
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import create_sync_engine
from app.worker.celery_app import celery_app

TASKS = (
    "app.worker.integrity.verify_audit_chain",
    "app.worker.integrity.verify_ledger_integrity",
    "app.worker.maintenance.sweep_expired_refresh_tokens",
    "app.worker.maintenance.prune_idempotency_keys",
)
TASK_TIMEOUT_SECONDS = 120


def main() -> int:
    settings = get_settings()
    engine = create_sync_engine(settings, purpose="worker-live-proof")
    run_id = str(int(time.time()))
    username = f"live-prov-{run_id}"
    user_id = str(uuid.uuid5(uuid.NAMESPACE_URL, username))
    token_hash = hashlib.sha256(f"live-proof-{run_id}".encode()).hexdigest()
    request_hash = hashlib.sha256(f"live-proof-req-{run_id}".encode()).hexdigest()

    # ``include=[...]`` in the Celery config is what a real worker resolves on
    # start-up; importing the default modules here registers the same tasks.
    celery_app.loader.import_default_modules()
    missing = [name for name in TASKS if name not in celery_app.tasks]
    if missing:
        print(f"FAIL: tasks are not registered: {missing}")
        return 1
    registered = len(celery_app.tasks)
    print(f"broker configured={bool(celery_app.conf.broker_url)} tasks registered={registered}")

    failures: list[str] = []
    with engine.begin() as conn:
        # An expired refresh token and a long-finished idempotency key: the two
        # maintenance tasks must find and clean up exactly these rows.
        conn.execute(
            text(
                "INSERT INTO users (id, username, full_name, password_hash, is_active) "
                "VALUES (:id, :username, 'Live Proof', 'argon2id$not-a-real-hash', true)"
            ),
            {"id": user_id, "username": username},
        )
        conn.execute(
            text(
                "INSERT INTO refresh_tokens "
                "(user_id, family_id, token_hash, issued_at, expires_at) "
                "VALUES (:uid, gen_random_uuid(), :token_hash, "
                "now() - interval '40 days', now() - interval '10 days')"
            ),
            {"uid": user_id, "token_hash": token_hash},
        )
        conn.execute(
            text(
                "INSERT INTO idempotency_keys (key, user_id, endpoint, request_hash, status, "
                "created_at, completed_at) VALUES (gen_random_uuid(), :uid, "
                "'/api/v1/exchange', :request_hash, 'COMPLETED', "
                "now() - interval '400 days', now() - interval '400 days')"
            ),
            {"uid": user_id, "request_hash": request_hash},
        )

    try:
        for name in TASKS:
            async_result = AsyncResult(celery_app.send_task(name).id, app=celery_app)
            payload = async_result.get(timeout=TASK_TIMEOUT_SECONDS)
            state = async_result.state
            rendered = json.dumps(payload, default=str, sort_keys=True)
            print(f"{name}\n  state={state}\n  result={rendered}")
            if state != "SUCCESS":
                failures.append(f"{name} ended in state {state}")

        with engine.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM refresh_tokens "
                    "  WHERE token_hash = :token_hash AND revoked_at IS NOT NULL) AS revoked, "
                    "(SELECT count(*) FROM idempotency_keys "
                    "  WHERE request_hash = :request_hash) AS leftover"
                ),
                {"token_hash": token_hash, "request_hash": request_hash},
            ).one()
        print(
            "after maintenance: "
            f"probe token revoked={row.revoked} probe idempotency row left={row.leftover}"
        )
        if row.revoked != 1:
            failures.append("sweep_expired_refresh_tokens did not revoke the expired probe token")
        if row.leftover != 0:
            failures.append("prune_idempotency_keys did not remove the expired probe key")
    finally:
        with engine.begin() as conn:
            # users are append-only (PART 22/25): deactivate, never delete.
            conn.execute(text("UPDATE users SET is_active = false WHERE id = :id"), {"id": user_id})
        engine.dispose()

    print(f"checked at {datetime.now(tz=UTC).isoformat()}")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        print("LIVE WORKER PROOF: FAILED")
        return 1
    print("LIVE WORKER PROOF: ALL TASKS SUCCEEDED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
