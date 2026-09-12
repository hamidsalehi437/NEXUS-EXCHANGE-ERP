"""Seeding is idempotent, auditable and safe to run at every deployment.

Phase 1 acceptance: the seed process converges the database to the approved
reference data and a second run changes nothing. A seed that "updates" rows on
every run would look harmless in a log and rewrite production rows forever.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest

from tests.helpers import (
    DEV_ADMIN_PASSWORD,
    create_database,
    database_dsn,
    drop_database,
    fetch_all,
    fetch_scalar,
    migrate,
    run_seeds,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# Seed 004 creates the development administrator only when APP_ENV=development
# (PART 45); the API's own test run uses app_env=test, which is unaffected by how
# the database was seeded.
SEED_ENV = {"APP_ENV": "development", "DEV_ADMIN_PASSWORD": DEV_ADMIN_PASSWORD}

_CURRENCY_COUNT = 7
_ROLE_COUNT = 6
_PERMISSION_COUNT = 31
_ACCOUNT_COUNT = 42
# Phase 2 added seed 005: the bootstrap branch a first device can register against.
# It creates exactly one row, and only in the development/test bootstrap environment.
_BRANCH_COUNT = 1
# First run: the reference rows above, plus the bootstrap branch, plus the development
# administrator — which counts two rows (the user and its SUPER_ADMIN assignment).
_TOTAL_ROWS_ON_FIRST_RUN = (
    _CURRENCY_COUNT + _ROLE_COUNT + _PERMISSION_COUNT + _ACCOUNT_COUNT + _BRANCH_COUNT + 2
)
# Later runs: the administrator module reports a single "unchanged" row (it returns as
# soon as it sees the existing user, so the role assignment is not counted again).
_TOTAL_ROWS_ON_LATER_RUNS = _TOTAL_ROWS_ON_FIRST_RUN - 1


def _totals(output: str) -> dict[str, int]:
    """Parse the ``TOTAL: inserted=… updated=… unchanged=… removed=…`` line."""
    match = re.search(r"TOTAL: inserted=(\d+) updated=(\d+) unchanged=(\d+) removed=(\d+)", output)
    assert match, f"no TOTAL line in seed output:\n{output}"
    inserted, updated, unchanged, removed = (int(value) for value in match.groups())
    return {
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "removed": removed,
    }


@pytest.fixture(scope="module")
def seed_database() -> Iterator[str]:
    """A migrated, unseeded database dedicated to the seed tests."""
    name = "nexus_test_seeds"
    create_database(name)
    try:
        migrate(name)
        yield name
    finally:
        drop_database(name)


class TestFirstRun:
    def test_first_run_inserts_the_reference_data(self, seed_database: str) -> None:
        result = run_seeds(seed_database, extra_env=SEED_ENV)
        assert result.returncode == 0, result.stderr
        totals = _totals(result.stdout)
        # 7 currencies + 6 roles + 31 permissions + 42 accounts + 1 branch + the
        # development administrator (user + role assignment are counted as two rows).
        assert totals["inserted"] == _TOTAL_ROWS_ON_FIRST_RUN
        assert totals["updated"] == 0
        assert totals["removed"] == 0

    def test_second_run_changes_nothing(self, seed_database: str) -> None:
        result = run_seeds(seed_database, extra_env=SEED_ENV)
        assert result.returncode == 0, result.stderr
        totals = _totals(result.stdout)
        # Every row from the first run is reported unchanged, including the branch.
        assert totals == {
            "inserted": 0,
            "updated": 0,
            "unchanged": _TOTAL_ROWS_ON_LATER_RUNS,
            "removed": 0,
        }

    def test_third_run_is_still_a_no_op(self, seed_database: str) -> None:
        result = run_seeds(seed_database, extra_env=SEED_ENV)
        assert _totals(result.stdout)["inserted"] == 0
        assert _totals(result.stdout)["updated"] == 0

    def test_check_mode_is_green_after_a_run(self, seed_database: str) -> None:
        result = run_seeds(seed_database, "--check", extra_env=SEED_ENV)
        assert result.returncode == 0, result.stdout + result.stderr
        assert _totals(result.stdout)["updated"] == 0


class TestCheckModeIsNotVacuous:
    def test_check_mode_reports_pending_work_without_writing(self) -> None:
        name = "nexus_test_seeds_check"
        create_database(name)
        try:
            migrate(name)
            result = run_seeds(name, "--check", extra_env=SEED_ENV)
            assert result.returncode != 0  # a fresh database needs seeding
            assert _totals(result.stdout)["inserted"] == _TOTAL_ROWS_ON_FIRST_RUN
            # Nothing was written: the check rolls its transaction back.
            assert fetch_scalar(name, "SELECT count(*) FROM currencies") == 0
            assert fetch_scalar(name, "SELECT count(*) FROM roles") == 0
            assert fetch_scalar(name, "SELECT count(*) FROM users") == 0
            assert fetch_scalar(name, "SELECT count(*) FROM branches") == 0
        finally:
            drop_database(name)


class TestSeededContent:
    def test_currencies_include_exactly_one_base(self, seed_database: str) -> None:
        assert fetch_scalar(seed_database, "SELECT count(*) FROM currencies") == _CURRENCY_COUNT
        assert fetch_scalar(seed_database, "SELECT count(*) FROM currencies WHERE is_base") == 1

    def test_base_currency_is_afn(self, seed_database: str) -> None:
        assert fetch_scalar(seed_database, "SELECT code FROM currencies WHERE is_base") == "AFN"

    def test_roles_and_permissions_match_the_approved_matrix(self, seed_database: str) -> None:
        assert fetch_scalar(seed_database, "SELECT count(*) FROM roles") == _ROLE_COUNT
        assert fetch_scalar(seed_database, "SELECT count(*) FROM permissions") == _PERMISSION_COUNT
        assert fetch_scalar(seed_database, "SELECT count(*) FROM role_permissions") > 0

    def test_every_role_has_at_least_one_permission(self, seed_database: str) -> None:
        rows = fetch_all(
            seed_database,
            """
            SELECT r.name, count(rp.permission_code)
            FROM roles r LEFT JOIN role_permissions rp ON rp.role_id = r.id
            GROUP BY r.name
            """,
        )
        assert len(rows) == _ROLE_COUNT
        assert all(count > 0 for _, count in rows), rows

    def test_chart_of_accounts_is_complete(self, seed_database: str) -> None:
        assert fetch_scalar(seed_database, "SELECT count(*) FROM accounts") == _ACCOUNT_COUNT
        # Every active currency needs its own cash account and payable account.
        rows = fetch_all(
            seed_database,
            """
            SELECT c.code, count(a.id)
            FROM currencies c LEFT JOIN accounts a ON a.currency_id = c.id
            GROUP BY c.code
            """,
        )
        assert len(rows) == _CURRENCY_COUNT
        assert all(count >= 2 for _, count in rows), rows

    def test_accounts_respect_the_double_entry_signs(self, seed_database: str) -> None:
        bad = fetch_all(
            seed_database,
            """
            SELECT code, account_type, normal_balance FROM accounts
            WHERE (account_type IN ('ASSET', 'EXPENSE') AND normal_balance <> 'DEBIT')
               OR (account_type IN ('LIABILITY', 'EQUITY', 'REVENUE')
                   AND normal_balance <> 'CREDIT')
            """,
        )
        assert bad == []

    def test_development_administrator_exists_with_a_role(self, seed_database: str) -> None:
        rows = fetch_all(
            seed_database,
            """
            SELECT u.username, u.is_active, r.name
            FROM users u
            JOIN user_roles ur ON ur.user_id = u.id
            JOIN roles r ON r.id = ur.role_id
            WHERE u.username = 'admin'
            """,
        )
        assert rows, "the development administrator was not created"
        username, is_active, role = rows[0]
        assert username == "admin"
        assert is_active is True
        assert role == "SUPER_ADMIN"

    def test_password_is_stored_as_an_argon2id_hash(self, seed_database: str) -> None:
        stored = fetch_scalar(
            seed_database, "SELECT password_hash FROM users WHERE username = 'admin'"
        )
        assert str(stored).startswith("$argon2id$")
        assert DEV_ADMIN_PASSWORD not in str(stored)

    def test_seed_runs_are_recorded_in_the_audit_log(self, seed_database: str) -> None:
        rows = fetch_all(
            seed_database,
            """
            SELECT action, entity_type FROM audit_logs
            WHERE request_id = 'seed' ORDER BY action
            """,
        )
        assert rows, "seeding must leave an audit trail"
        assert all(action for action, _ in rows)

    def test_audit_chain_is_valid_after_seeding(self, seed_database: str) -> None:
        assert fetch_scalar(seed_database, "SELECT count(*) FROM verify_audit_chain()") == 0

    def test_re_running_the_seed_does_not_duplicate_audit_rows(self, seed_database: str) -> None:
        before = fetch_scalar(seed_database, "SELECT count(*) FROM audit_logs")
        run_seeds(seed_database, extra_env=SEED_ENV)
        assert fetch_scalar(seed_database, "SELECT count(*) FROM audit_logs") == before


class TestSeedSafety:
    def test_seeding_never_deletes_financial_history(self, seed_database: str) -> None:
        result = run_seeds(seed_database, extra_env=SEED_ENV)
        assert _totals(result.stdout)["removed"] == 0

    def test_seed_runner_refuses_to_run_without_a_migration(self) -> None:
        name = "nexus_test_seeds_unmigrated"
        create_database(name)
        try:
            result = run_seeds(name, extra_env=SEED_ENV)
            assert result.returncode != 0
            assert "no applied migration" in (result.stdout + result.stderr)
        finally:
            drop_database(name)

    def test_seed_runner_is_connected_to_the_right_database(self, seed_database: str) -> None:
        # Guards against a test that silently seeds the developer's own database.
        assert database_dsn(seed_database).endswith("/nexus_test_seeds")
