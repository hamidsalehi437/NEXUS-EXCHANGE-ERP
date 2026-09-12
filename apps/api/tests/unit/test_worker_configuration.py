"""The worker's configuration is part of the reliability contract (PART 44).

A task that acknowledges before it finishes can be lost; a verification that runs
forever can block maintenance. Both are configuration, so both are tested.
"""

from __future__ import annotations

import pytest

from app.worker.celery_app import (
    DEFAULT_QUEUE,
    WORKER_QUEUES,
    _redacted,
    celery_app,
)

pytestmark = pytest.mark.unit

INTEGRITY_TASKS = {
    "app.worker.integrity.verify_audit_chain",
    "app.worker.integrity.verify_ledger_integrity",
}
MAINTENANCE_TASKS = {
    "app.worker.maintenance.sweep_expired_refresh_tokens",
    "app.worker.maintenance.prune_idempotency_keys",
}
BEAT_KEYS = {
    "verify-audit-chain",
    "verify-ledger-integrity",
    "sweep-expired-refresh-tokens",
    "prune-idempotency-keys",
}


@pytest.fixture(scope="module", autouse=True)
def _load_tasks() -> None:
    """Load the modules named in ``include``, exactly as a worker does at startup."""
    celery_app.loader.import_default_modules()


class TestTimeAndDeliveryGuarantees:
    def test_everything_is_utc(self) -> None:
        assert celery_app.conf.timezone == "UTC"
        assert celery_app.conf.enable_utc is True

    def test_tasks_are_acknowledged_after_they_finish(self) -> None:
        assert celery_app.conf.task_acks_late is True
        assert celery_app.conf.task_reject_on_worker_lost is True

    def test_a_worker_takes_one_task_at_a_time(self) -> None:
        assert celery_app.conf.worker_prefetch_multiplier == 1

    def test_time_limits_are_ordered_and_bounded(self) -> None:
        soft = celery_app.conf.task_soft_time_limit
        hard = celery_app.conf.task_time_limit
        assert 0 < soft < hard

    def test_serialisation_is_json_only(self) -> None:
        assert celery_app.conf.task_serializer == "json"
        assert celery_app.conf.result_serializer == "json"
        assert list(celery_app.conf.accept_content) == ["json"]


class TestTaskRegistry:
    def test_integrity_tasks_are_registered(self) -> None:
        assert set(celery_app.tasks) >= INTEGRITY_TASKS

    def test_maintenance_tasks_are_registered(self) -> None:
        assert set(celery_app.tasks) >= MAINTENANCE_TASKS

    def test_no_financial_write_task_exists(self) -> None:
        # PART 47: money moves inside an API request transaction, never in a task.
        writers = [name for name in celery_app.tasks if "post" in name or "create" in name]
        assert writers == []


class TestQueuesAndRouting:
    def test_default_queue_is_maintenance(self) -> None:
        assert DEFAULT_QUEUE == "maintenance"
        assert celery_app.conf.task_default_queue == DEFAULT_QUEUE

    def test_worker_consumes_both_queues(self) -> None:
        assert WORKER_QUEUES == ("integrity", "maintenance")

    def test_routing_sends_checks_and_upkeep_to_separate_queues(self) -> None:
        routes = celery_app.conf.task_routes
        assert routes["app.worker.integrity.*"]["queue"] == "integrity"
        assert routes["app.worker.maintenance.*"]["queue"] == "maintenance"

    def test_every_queue_used_by_beat_is_declared(self) -> None:
        queues = set(celery_app.conf.task_queues)
        declared = {entry["options"]["queue"] for entry in celery_app.conf.beat_schedule.values()}
        assert declared <= queues
        assert queues == set(WORKER_QUEUES)


class TestBeatSchedule:
    def test_schedule_contains_exactly_the_four_checks(self) -> None:
        assert set(celery_app.conf.beat_schedule) == BEAT_KEYS

    def test_each_schedule_entry_names_a_registered_task(self) -> None:
        for entry in celery_app.conf.beat_schedule.values():
            assert entry["task"] in celery_app.tasks
            assert entry["schedule"] is not None

    def test_integrity_checks_run_on_the_integrity_queue(self) -> None:
        schedule = celery_app.conf.beat_schedule
        assert schedule["verify-audit-chain"]["options"]["queue"] == "integrity"
        assert schedule["verify-ledger-integrity"]["options"]["queue"] == "integrity"

    def test_maintenance_runs_on_the_maintenance_queue(self) -> None:
        schedule = celery_app.conf.beat_schedule
        assert schedule["sweep-expired-refresh-tokens"]["options"]["queue"] == "maintenance"
        assert schedule["prune-idempotency-keys"]["options"]["queue"] == "maintenance"

    def test_every_scheduled_task_expires(self) -> None:
        # A queue that was down for a week must not replay a week of checks at once.
        for entry in celery_app.conf.beat_schedule.values():
            assert entry["options"]["expires"] > 0


class TestStartupLogRedaction:
    def test_credentials_are_stripped_from_broker_urls(self) -> None:
        assert _redacted("redis://:sup3rsecret@redis:6379/1") == "redis://***@redis:6379/1"

    def test_credentials_are_stripped_from_backend_urls(self) -> None:
        assert _redacted("rediss://user:pw@cache:6380/2") == "rediss://***@cache:6380/2"

    def test_url_without_credentials_is_unchanged(self) -> None:
        assert _redacted("redis://redis:6379/0") == "redis://redis:6379/0"
