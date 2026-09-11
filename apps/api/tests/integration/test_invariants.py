"""The Phase 0 financial invariants still hold after the Phase 1 migration.

The Phase 0 suite (``tests/invariants/phase0_schema_invariants.sql``) is executed by
this test against a database built by the migration, and a negative control proves
the suite is not vacuous. Read-only checks then re-assert the same invariants on the
live, seeded database the API runs against.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from tests.helpers import (
    REPO_ROOT,
    create_database,
    database_dsn,
    drop_database,
    fetch_scalar,
    migrate,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

INVARIANT_SUITE = REPO_ROOT / "tests" / "invariants" / "phase0_schema_invariants.sql"
PASSED_BANNER = "PHASE 0 SCHEMA INVARIANT SUITE: ALL ASSERTIONS PASSED"


def run_sql_script(engine: Engine, script_path: object) -> list[str]:
    """Execute a psql script through the driver and return its NOTICE messages.

    ``psql`` meta-commands (``\\set``/``\\pset``) are stripped; everything else is
    executed unchanged as one simple-protocol script, so the SQL under test is exactly
    the reviewed file. NOTICEs are captured so the suite's ``PASS`` lines are visible.
    """
    from pathlib import Path

    script = Path(str(script_path)).read_text(encoding="utf-8")
    sql = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("\\"))
    notices: list[str] = []
    # AUTOCOMMIT mirrors psql: the script manages its own transactions (it contains
    # BEGIN/COMMIT blocks), which would otherwise nest inside an outer transaction.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        driver_connection = connection.connection.driver_connection  # type: ignore[attr-defined]
        driver_connection.add_notice_handler(
            lambda diagnostic: notices.append(diagnostic.message_primary or "")
        )
        with driver_connection.cursor() as cursor:
            cursor.execute(sql)
    return notices


@pytest.fixture(scope="module")
def suite_run() -> Iterator[tuple[Engine, list[str]]]:
    """One fresh database, migrated, with the invariant suite executed once.

    The suite provisions its own fixtures (deterministic UUIDs, committed) and is
    therefore designed to run against a fresh database — re-running it in place is
    covered by ``TestSuitePreconditions``.
    """
    name = "nexus_test_invariants"
    create_database(name)
    migrate(name)
    engine = create_engine(database_dsn(name), future=True)
    try:
        yield engine, run_sql_script(engine, INVARIANT_SUITE)
    finally:
        engine.dispose()
        drop_database(name)


@pytest.fixture(scope="module")
def tampered_engine() -> Iterator[Engine]:
    """A fresh database whose audit chain is broken *before* the suite runs."""
    name = "nexus_test_invariants_tamper"
    create_database(name)
    migrate(name)
    engine = create_engine(database_dsn(name), future=True)
    try:
        _append_and_corrupt_audit_row(engine)
        yield engine
    finally:
        engine.dispose()
        drop_database(name)


def _append_and_corrupt_audit_row(engine: Engine) -> None:
    """Write one audit row through the chain trigger, then corrupt it behind the guards.

    Disabling the append-only triggers is what an attacker with database access would
    do, and it is exactly the situation the tamper-evident chain exists to expose.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO audit_logs (action, entity_type, new_data, request_id)
                VALUES ('PROBE_WRITTEN', 'probe', '{"value": 1}'::jsonb, 'invariant-test')
                """
            )
        )
        connection.execute(text("ALTER TABLE audit_logs DISABLE TRIGGER USER"))
        connection.execute(
            text(
                """
                UPDATE audit_logs
                   SET new_data = '{"value": 2}'::jsonb
                 WHERE request_id = 'invariant-test'
                """
            )
        )


class TestPhase0SuiteOnMigratedDatabase:
    def test_every_assertion_passes(self, suite_run: tuple[Engine, list[str]]) -> None:
        _, notices = suite_run
        assert any(PASSED_BANNER in notice for notice in notices), "\n".join(notices)

    def test_the_suite_really_asserts_things(self, suite_run: tuple[Engine, list[str]]) -> None:
        _, notices = suite_run
        passes = [notice for notice in notices if notice.startswith("PASS")]
        assert len(passes) >= 50, f"only {len(passes)} assertions reported"

    def test_the_suite_covers_all_eight_invariant_groups(
        self, suite_run: tuple[Engine, list[str]]
    ) -> None:
        _, notices = suite_run
        covered = {
            notice.split()[1].split("/")[0].rstrip(":")
            for notice in notices
            if notice.startswith("PASS")
        }
        for identifier in ("I-1", "I-2", "I-3", "I-4", "I-5", "I-6", "I-7", "I-8"):
            assert identifier in covered, f"{identifier} missing from the suite output"

    def test_ledger_totals_are_equal_and_reported(
        self, suite_run: tuple[Engine, list[str]]
    ) -> None:
        _, notices = suite_run
        assert any("ledger totals" in notice for notice in notices)

    def test_the_suite_leaves_the_schema_intact(self, suite_run: tuple[Engine, list[str]]) -> None:
        engine, _ = suite_run
        with engine.connect() as connection:
            count = connection.execute(
                text(
                    """
                    SELECT count(*) FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relkind = 'r'
                    """
                )
            ).scalar_one()
        assert count == 32


class TestSuitePreconditions:
    """The suite is a Phase 0 artefact with a documented precondition:

    it provisions its own fixtures and is designed to run against a *fresh* database.
    Running it twice in place collides on those fixtures. The test suite re-runs it on
    a new database instead (the ``suite_run`` fixture), which is how CI and operators
    are told to use it.
    """

    def test_second_run_on_the_same_database_collides(
        self, suite_run: tuple[Engine, list[str]]
    ) -> None:
        engine, _ = suite_run
        with pytest.raises(Exception) as failure:
            run_sql_script(engine, INVARIANT_SUITE)
        assert "already exists" in str(failure.value)


class TestTheSuiteIsNotVacuous:
    def test_a_broken_audit_chain_fails_the_suite(self, tampered_engine: Engine) -> None:
        with pytest.raises(Exception) as failure:
            run_sql_script(tampered_engine, INVARIANT_SUITE)
        message = str(failure.value).lower()
        assert "i-6" in message and "chain" in message, message


class TestInvariantsOnTheSeededDatabase:
    """Read-only checks against the database the API under test uses."""

    def test_no_journal_entry_is_unbalanced(self, main_database: str) -> None:
        unbalanced = fetch_scalar(
            main_database,
            """
            SELECT count(*) FROM (
                SELECT journal_entry_id
                FROM journal_lines
                GROUP BY journal_entry_id
                HAVING SUM(debit) <> SUM(credit)
            ) AS unbalanced_entries
            """,
        )
        assert unbalanced == 0

    def test_total_debit_equals_total_credit(self, main_database: str) -> None:
        debit, credit = _totals(main_database)
        assert debit == credit

    def test_every_entry_has_at_least_two_lines(self, main_database: str) -> None:
        rows = fetch_scalar(
            main_database,
            """
            SELECT count(*) FROM (
                SELECT journal_entry_id
                FROM journal_lines
                GROUP BY journal_entry_id
                HAVING count(*) < 2
            ) AS short_entries
            """,
        )
        assert rows == 0

    def test_the_balance_cache_matches_the_journal(self, main_database: str) -> None:
        mismatches = fetch_scalar(
            main_database,
            """
            SELECT count(*)
            FROM account_balances b
            WHERE (b.debit_total, b.credit_total) <> (
                SELECT COALESCE(SUM(l.debit), 0), COALESCE(SUM(l.credit), 0)
                FROM journal_lines l
                WHERE l.account_id = b.account_id AND l.currency_id = b.currency_id
            )
            """,
        )
        assert mismatches == 0

    def test_the_audit_chain_is_intact(self, main_database: str) -> None:
        assert fetch_scalar(main_database, "SELECT count(*) FROM verify_audit_chain()") == 0

    def test_no_audit_row_is_ever_deleted(self, main_database: str) -> None:
        # The append-only triggers must be enabled (not left disabled by anything).
        disabled = fetch_scalar(
            main_database,
            """
            SELECT count(*) FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            WHERE NOT t.tgisinternal AND t.tgenabled = 'D'
            """,
        )
        assert disabled == 0

    def test_journal_lines_reference_posting_accounts(self, main_database: str) -> None:
        orphan_lines = fetch_scalar(
            main_database,
            """
            SELECT count(*) FROM journal_lines l
            LEFT JOIN accounts a ON a.id = l.account_id
            WHERE a.id IS NULL
            """,
        )
        assert orphan_lines == 0

    def test_money_is_never_stored_as_a_float(self, main_database: str) -> None:
        floats = fetch_scalar(
            main_database,
            """
            SELECT count(*) FROM information_schema.columns
            WHERE table_schema = 'public'
              AND data_type IN ('real', 'double precision')
            """,
        )
        assert floats == 0


def _totals(database: str) -> tuple[object, object]:
    from tests.helpers import fetch_all

    rows = fetch_all(database, "SELECT SUM(debit), SUM(credit) FROM journal_lines")
    return rows[0][0], rows[0][1]
