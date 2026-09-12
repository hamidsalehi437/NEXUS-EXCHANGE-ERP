"""The Phase 1 schema gates, exercised against real databases.

``orm-db``   the SQLAlchemy metadata mirrors the migrated database
``db-db``    the migration result is structurally identical to the approved DDL

Both gates are also negative-controlled here: a deliberately introduced drift must
be detected, otherwise the gate would be decoration.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from scripts.schema_gate import (
    Column,
    DatabaseSpec,
    IndexSpec,
    TableSpec,
    diff,
    diff_objects,
    orm_spec,
    reflect_spec,
)
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from tests.helpers import (
    API_ROOT,
    REFERENCE_SCHEMA,
    create_database,
    database_dsn,
    drop_database,
    execute_sql_file,
    migrate,
)

pytestmark = [pytest.mark.integration, pytest.mark.schema]


@pytest.fixture(scope="module")
def migrated_dsn() -> Iterator[str]:
    """A database built by the migration, named so parallel runs cannot collide."""
    name = "nexus_test_gate_migrated"
    create_database(name)
    try:
        migrate(name)
        yield database_dsn(name)
    finally:
        drop_database(name)


@pytest.fixture(scope="module")
def reference_dsn() -> Iterator[str]:
    """A database built by applying ``docs/database/schema.sql`` verbatim."""
    name = "nexus_test_gate_reference"
    create_database(name)
    engine = create_engine(database_dsn(name), future=True)
    try:
        execute_sql_file(engine, REFERENCE_SCHEMA)
        yield database_dsn(name)
    finally:
        engine.dispose()
        drop_database(name)


def _spec(table: str, column: Column) -> DatabaseSpec:
    """Build a one-table structure for comparator tests."""
    spec = DatabaseSpec()
    spec.tables[table] = TableSpec(name=table, columns={column.name: column})
    return spec


class TestOrmDatabaseGate:
    def test_orm_metadata_matches_the_migrated_database(self, migrated_dsn: str) -> None:
        engine = create_engine(migrated_dsn, future=True)
        try:
            database = reflect_spec(engine)
        finally:
            engine.dispose()
        problems = diff(orm_spec(), database, left_name="ORM", right_name="DATABASE")
        assert problems == []

    def test_orm_covers_every_table(self, migrated_dsn: str) -> None:
        engine = create_engine(migrated_dsn, future=True)
        try:
            database = reflect_spec(engine)
        finally:
            engine.dispose()
        assert set(orm_spec().tables) == set(database.tables)
        assert len(database.tables) == 31

    def test_gate_detects_an_extra_column(self, migrated_dsn: str) -> None:
        """Negative control: a real difference must be reported."""
        engine = create_engine(migrated_dsn, future=True)
        try:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE currencies ADD COLUMN gate_probe TEXT"))
            tampered = reflect_spec(engine)
            problems = diff(orm_spec(), tampered, left_name="ORM", right_name="DATABASE")
            assert any("gate_probe" in problem for problem in problems)
        finally:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE currencies DROP COLUMN gate_probe"))
            engine.dispose()


class TestReferenceDatabaseGate:
    def test_migration_result_matches_the_approved_ddl(
        self, reference_dsn: str, migrated_dsn: str
    ) -> None:
        left_engine, right_engine = create_engine(reference_dsn), create_engine(migrated_dsn)
        try:
            reference = reflect_spec(left_engine, with_objects=True)
            migrated = reflect_spec(right_engine, with_objects=True)
        finally:
            left_engine.dispose()
            right_engine.dispose()

        problems = diff(reference, migrated, left_name="REFERENCE", right_name="MIGRATION")
        problems.extend(diff_objects(reference, migrated))
        assert problems == []

    def test_every_object_class_is_present(self, migrated_dsn: str) -> None:
        engine = create_engine(migrated_dsn, future=True)
        try:
            spec = reflect_spec(engine, with_objects=True)
        finally:
            engine.dispose()
        assert len(spec.tables) == 31
        assert len(spec.views) == 5
        assert len(spec.functions) == 23
        assert len(spec.triggers) == 48
        assert len(spec.indexes) == 72
        assert len(spec.checks) == 71  # every business rule is a database constraint

    def test_gate_detects_a_dropped_trigger(self, reference_dsn: str, migrated_dsn: str) -> None:
        """Negative control: a missing immutability trigger must be reported."""
        engine = create_engine(migrated_dsn, future=True)
        try:
            with engine.begin() as connection:
                connection.execute(text("DROP TRIGGER trg_branches_no_delete ON branches"))
            reference_engine = create_engine(reference_dsn, future=True)
            try:
                reference = reflect_spec(reference_engine, with_objects=True)
            finally:
                reference_engine.dispose()
            tampered = reflect_spec(engine, with_objects=True)
            problems = diff_objects(reference, tampered)
            assert any("trg_branches_no_delete" in problem for problem in problems)
        finally:
            engine.dispose()

    def test_gate_detects_a_broken_money_type(self, reference_dsn: str, migrated_dsn: str) -> None:
        """Negative control: swapping a NUMERIC money column for a float must fail.

        ``device_allocations.granted_amount`` is used because PostgreSQL refuses to
        alter a column that a generated column or a view depends on — a second line
        of defence that this test documents by choosing a free column.
        """
        engine = create_engine(migrated_dsn, future=True)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE device_allocations "
                        "ALTER COLUMN granted_amount TYPE DOUBLE PRECISION"
                    )
                )
            reference_engine = create_engine(reference_dsn, future=True)
            try:
                reference = reflect_spec(reference_engine)
            finally:
                reference_engine.dispose()
            tampered = reflect_spec(engine)
            problems = diff(reference, tampered, left_name="REFERENCE", right_name="MIGRATION")
            assert any(
                "granted_amount" in problem and "DOUBLE" in problem.upper() for problem in problems
            )
        finally:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE device_allocations "
                        "ALTER COLUMN granted_amount TYPE NUMERIC(30, 10)"
                    )
                )
            engine.dispose()


class TestComparatorLogic:
    """The comparator must catch each class of drift, not only the ones a live
    database lets us create (PostgreSQL refuses some destructive ALTERs outright)."""

    def test_type_change_is_reported(self) -> None:
        left = _spec("accounts", Column("amount", "NUMERIC(30, 10)", False, None, False))
        right = _spec("accounts", Column("amount", "DOUBLE PRECISION", False, None, False))
        problems = diff(left, right, left_name="ORM", right_name="DB")
        assert any("TYPE" in problem for problem in problems)

    def test_nullability_change_is_reported(self) -> None:
        left = _spec("accounts", Column("code", "VARCHAR(20)", False, None, False))
        right = _spec("accounts", Column("code", "VARCHAR(20)", True, None, False))
        problems = diff(left, right, left_name="ORM", right_name="DB")
        assert any("NULLABLE" in problem for problem in problems)

    def test_orm_only_default_is_reported(self) -> None:
        # The dangerous direction: the ORM believes in a value the database never makes.
        left = _spec("accounts", Column("code", "VARCHAR(20)", False, "0", False))
        right = _spec("accounts", Column("code", "VARCHAR(20)", False, None, False))
        problems = diff(left, right, left_name="ORM", right_name="DB")
        assert any("DEFAULT" in problem for problem in problems)

    def test_database_only_default_is_not_reported(self) -> None:
        left = _spec("accounts", Column("code", "VARCHAR(20)", False, None, False))
        right = _spec("accounts", Column("code", "VARCHAR(20)", False, "now()", False))
        assert diff(left, right, left_name="ORM", right_name="DB") == []

    def test_missing_and_extra_tables_are_reported(self) -> None:
        left = _spec("accounts", Column("code", "VARCHAR(20)", False, None, False))
        right = DatabaseSpec()
        problems = diff(left, right, left_name="ORM", right_name="DB")
        assert any("MISSING_TABLE" in problem for problem in problems)

    def test_generated_column_difference_is_reported(self) -> None:
        left = _spec(
            "cash_movements", Column("signed_amount", "NUMERIC(30, 10)", False, None, True)
        )
        right = _spec(
            "cash_movements", Column("signed_amount", "NUMERIC(30, 10)", False, None, False)
        )
        problems = diff(left, right, left_name="ORM", right_name="DB")
        assert any("GENERATED" in problem for problem in problems)

    def test_missing_view_is_reported(self) -> None:
        left, right = DatabaseSpec(), DatabaseSpec()
        left.views["v_probe"] = "select 1"
        assert any("MISSING_VIEW" in problem for problem in diff_objects(left, right))

    def test_missing_index_is_reported(self) -> None:
        left, right = DatabaseSpec(), DatabaseSpec()
        left.indexes["ix_probe"] = IndexSpec("ix_probe", "accounts", ("code",), False, None)
        assert any("MISSING_INDEX" in problem for problem in diff_objects(left, right))

    def test_check_definition_change_is_reported(self) -> None:
        left, right = DatabaseSpec(), DatabaseSpec()
        left.checks["ck_probe"] = "amount > 0"
        right.checks["ck_probe"] = "amount >= 0"
        assert any("CHECK_DEFINITION" in problem for problem in diff_objects(left, right))


class TestGateConfiguration:
    def test_cli_module_is_importable(self) -> None:
        from scripts import schema_gate

        assert callable(schema_gate.main)

    def test_default_dsn_comes_from_settings(self) -> None:
        dsn = get_settings().migration_dsn_psycopg
        assert dsn.startswith("postgresql+psycopg://")

    def test_gate_shipped_in_the_expected_location(self) -> None:
        assert (API_ROOT / "scripts" / "schema_gate.py").is_file()
