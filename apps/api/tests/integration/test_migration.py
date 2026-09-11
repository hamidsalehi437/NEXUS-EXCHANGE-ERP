"""Alembic migrations run against a real, empty PostgreSQL 16 database.

Phase 1 acceptance: ``alembic upgrade head`` succeeds on a clean database, the
result matches the approved schema, the frozen DDL cannot be tampered with, and
the whole thing can be torn down and rebuilt.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.helpers import (
    API_ROOT,
    fetch_all,
    fetch_scalar,
    run_alembic,
    table_names,
)

pytestmark = [pytest.mark.integration, pytest.mark.schema, pytest.mark.slow]

SCHEMA_COPY = API_ROOT / "alembic" / "sql" / "0001_initial_schema.sql"
REFERENCE = API_ROOT.parent.parent / "docs" / "database" / "schema.sql"
CHECKSUMS = API_ROOT / "alembic" / "sql" / "CHECKSUMS.txt"
REVISION = "0001_initial_schema"


def _recorded_checksums() -> dict[str, str]:
    """Parse ``CHECKSUMS.txt`` (``<sha256>  <path>``, paths relative to apps/api/)."""
    entries: dict[str, str] = {}
    for line in CHECKSUMS.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        digest, _, path = stripped.partition("  ")
        entries[path.strip()] = digest.strip()
    return entries


class TestFreshDatabase:
    def test_upgrade_head_succeeds(self, scratch_database: object) -> None:
        database = scratch_database("nexus_test_migration_a")  # type: ignore[operator]
        assert table_names(database)

    def test_applied_revision_is_recorded(self, scratch_database: object) -> None:
        database = scratch_database("nexus_test_migration_b")  # type: ignore[operator]
        assert fetch_scalar(database, "SELECT version_num FROM alembic_version") == REVISION

    def test_schema_objects_exist(self, scratch_database: object) -> None:
        database = scratch_database("nexus_test_migration_c")  # type: ignore[operator]
        views = fetch_scalar(
            database,
            """
            SELECT count(*) FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'v' AND n.nspname = 'public'
            """,
        )
        triggers = fetch_scalar(
            database,
            """
            SELECT count(*) FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE NOT t.tgisinternal AND n.nspname = 'public'
            """,
        )
        routines = fetch_scalar(
            database,
            """
            SELECT count(*) FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public'
            """,
        )
        assert views == 5
        assert triggers == 48
        assert routines == 23

    def test_tables_are_exactly_the_approved_set(self, scratch_database: object) -> None:
        database = scratch_database("nexus_test_migration_d")  # type: ignore[operator]
        tables = table_names(database)
        assert len(tables) == 32  # 31 NEXUS tables + alembic_version
        assert "alembic_version" in tables
        for expected in ("accounts", "journal_lines", "audit_logs", "cash_movements"):
            assert expected in tables

    def test_money_columns_are_numeric(self, scratch_database: object) -> None:
        database = scratch_database("nexus_test_migration_e")  # type: ignore[operator]
        rows = fetch_all(
            database,
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND column_name IN (
                  'amount', 'debit', 'credit', 'rate', 'commission_amount',
                  'expected_amount', 'counted_amount', 'difference_amount'
              )
            """,
        )
        assert rows, "expected monetary columns to exist"
        offenders = [row for row in rows if row[2] != "numeric"]
        assert offenders == []

    def test_no_float_column_exists_anywhere(self, scratch_database: object) -> None:
        database = scratch_database("nexus_test_migration_f")  # type: ignore[operator]
        floats = fetch_all(
            database,
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND data_type IN ('real', 'double precision')
            """,
        )
        assert floats == []


class TestFrozenSchemaIntegrity:
    def test_schema_copy_is_byte_identical_to_the_reference(self) -> None:
        copy = SCHEMA_COPY.read_bytes()
        reference = REFERENCE.read_bytes()
        assert hashlib.sha256(copy).hexdigest() == hashlib.sha256(reference).hexdigest()

    def test_checksum_file_matches_both_schema_files(self) -> None:
        recorded = _recorded_checksums()
        assert (
            hashlib.sha256(SCHEMA_COPY.read_bytes()).hexdigest()
            == recorded["alembic/sql/0001_initial_schema.sql"]
        )
        assert (
            hashlib.sha256(REFERENCE.read_bytes()).hexdigest()
            == recorded["../../docs/database/schema.sql"]
        )

    def test_sha256sum_command_verifies_the_recorded_checksums(self) -> None:
        """The documented operator command must succeed as written."""
        sha256sum = shutil.which("sha256sum")
        if sha256sum is None:
            pytest.skip("sha256sum is not installed in this environment")
        result = subprocess.run(
            [sha256sum, "-c", "alembic/sql/CHECKSUMS.txt"],
            cwd=API_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "FAILED" not in result.stdout

    def test_revision_pins_the_same_checksum(self) -> None:
        revision_source = (
            API_ROOT / "alembic" / "versions" / "20260911_1400_0001_initial_schema.py"
        ).read_text(encoding="utf-8")
        digest = hashlib.sha256(SCHEMA_COPY.read_bytes()).hexdigest()
        assert digest in revision_source

    def test_tampered_ddl_aborts_the_migration_without_writing(
        self, scratch_database: object
    ) -> None:
        """A modified schema file must be refused before anything reaches the database."""
        database = scratch_database("nexus_test_migration_tamper", do_migrate=False)  # type: ignore[operator]
        original = SCHEMA_COPY.read_bytes()
        try:
            SCHEMA_COPY.write_bytes(original + b"\n-- unauthorised change\n")
            result = run_alembic(database, "upgrade", "head")
        finally:
            SCHEMA_COPY.write_bytes(original)

        assert result.returncode != 0
        assert "checksum" in (result.stderr + result.stdout).lower()
        assert table_names(database) == set()  # nothing was created

    def test_restored_file_migrates_again(self, scratch_database: object) -> None:
        database = scratch_database("nexus_test_migration_restore")  # type: ignore[operator]
        assert fetch_scalar(database, "SELECT version_num FROM alembic_version") == REVISION


class TestDowngradeAndReUpgrade:
    def test_downgrade_removes_the_schema_and_upgrade_rebuilds_it(self) -> None:
        """Uses its own database because it exercises the migration in both directions."""
        from tests.helpers import create_database, drop_database

        database = "nexus_test_migration_cycle"
        create_database(database)
        try:
            assert run_alembic(database, "upgrade", "head").returncode == 0
            before = table_names(database)

            down = run_alembic(database, "downgrade", "base")
            assert down.returncode == 0, down.stderr
            remaining = table_names(database)
            assert remaining == {"alembic_version"}, remaining

            up = run_alembic(database, "upgrade", "head")
            assert up.returncode == 0, up.stderr
            assert table_names(database) == before
        finally:
            drop_database(database)


class TestMigrationIsIdempotentUnderRepeatedRuns:
    def test_upgrade_head_twice_is_a_no_op(self, scratch_database: object) -> None:
        database = scratch_database("nexus_test_migration_twice")  # type: ignore[operator]
        before = table_names(database)
        second = run_alembic(database, "upgrade", "head")
        assert second.returncode == 0
        assert table_names(database) == before
        assert fetch_scalar(database, "SELECT version_num FROM alembic_version") == REVISION


class TestPathSafety:
    def test_checkout_paths_point_at_real_files(self) -> None:
        for path in (SCHEMA_COPY, REFERENCE, CHECKSUMS):
            assert isinstance(path, Path) and path.is_file(), path
