"""Schema gates: the ORM, the migration and the frozen schema must agree.

Two independent, read-only comparisons:

``orm-db``
    The SQLAlchemy metadata mirrors the database the Alembic migration builds:
    tables, every column (name, type, nullability, generated-ness), primary keys,
    foreign keys and unique constraints.

``db-db``
    The database built from ``docs/database/schema.sql`` (the frozen, reviewed
    reference) is structurally identical to the database built by the migration:
    the same tables, columns, keys, indexes, CHECK constraints, triggers,
    functions and views.

Design notes
------------
* **The database owns the expressions.** CHECK constraints, index predicates and
  generated-column expressions are not duplicated in the ORM; they are verified
  here (DB-to-DB, text and structure) and executed by the invariant suite
  (``tests/invariants/phase0_schema_invariants.sql``). The migration executes the
  frozen DDL verbatim, and the DDL is checksum-pinned, so an expression cannot
  drift without the checksum gate failing first.
* **Defaults fail in the dangerous direction only.** If the ORM declares a default
  the database does not have, the application would insert a value the database
  would never produce: that is an error. A database default the ORM does not
  declare is fine — the database fills it in.

Usage::

    python -m scripts.schema_gate orm-db
    python -m scripts.schema_gate db-db --left <dsn> --right <dsn>
    python -m scripts.schema_gate db-db --left <dsn> --right <dsn> --verbose
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, Table, create_engine, inspect, text
from sqlalchemy.dialects import postgresql

# Objects that belong to the tooling, not to the NEXUS schema.
TOOL_TABLES = frozenset({"alembic_version"})

_PG_DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]

_CAST = re.compile(r"::[a-z_ ]+(\(\d+(,\s*\d+)?\))?")
_WHITESPACE = re.compile(r"\s+")


def canonical_type(column_type: Any) -> str:
    """Compile a column type against the PostgreSQL dialect for comparison."""
    return str(column_type.compile(dialect=_PG_DIALECT)).upper()


def normalize_sql(expression: Any) -> str:
    """Canonicalise a SQL fragment so formatting differences do not matter."""
    if expression is None:
        return ""
    rendered = _WHITESPACE.sub(" ", str(expression)).strip().rstrip(";")
    return rendered.lower()


def normalize_default(expression: Any) -> str:
    """Reduce a column default to a comparable literal where possible."""
    if expression is None:
        return ""
    rendered = normalize_sql(expression)
    rendered = _CAST.sub("", rendered)
    rendered = rendered.strip("()")
    if rendered.startswith("'") and rendered.endswith("'"):
        rendered = rendered[1:-1]
    if rendered in {"current_timestamp", "now()"}:
        return "now()"
    if rendered in {"uuid_generate_v4()", "gen_random_uuid()"}:
        return "gen_random_uuid()"
    return rendered


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool
    default: str | None
    computed: bool
    identity: bool = False


@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[str, ...]
    referred_table: str
    referred_columns: tuple[str, ...]
    ondelete: str | None


@dataclass
class TableSpec:
    name: str
    columns: dict[str, Column] = field(default_factory=dict)
    primary_key: tuple[str, ...] = ()
    foreign_keys: tuple[ForeignKey, ...] = ()
    unique_sets: tuple[frozenset[str], ...] = ()


@dataclass
class IndexSpec:
    """One index as it exists in the database.

    ``columns`` holds ``None`` for an expression member (for example
    ``lower(currency_code)``): PostgreSQL reflects those with no column name, and the
    DB-to-DB gate compares them through the index definition instead.
    """

    name: str
    table: str
    columns: tuple[str | None, ...]
    unique: bool
    predicate: str | None


@dataclass
class DatabaseSpec:
    tables: dict[str, TableSpec] = field(default_factory=dict)
    indexes: dict[str, IndexSpec] = field(default_factory=dict)
    checks: dict[str, str] = field(default_factory=dict)
    triggers: dict[str, str] = field(default_factory=dict)
    functions: set[str] = field(default_factory=set)
    views: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- ORM
def _orm_default(column: Any) -> str | None:
    """The ORM's declared server-side default, normalised for comparison."""
    server_default = column.server_default
    if server_default is None:
        return None
    argument = getattr(server_default, "arg", None)
    if argument is None:
        return "database-supplied"  # FetchedValue: identity sequence or trigger
    return normalize_default(argument)


def _metadata_table(table: Table) -> TableSpec:
    spec = TableSpec(name=table.name)
    for column in table.columns:
        spec.columns[column.name] = Column(
            name=column.name,
            type=canonical_type(column.type),
            nullable=bool(column.nullable),
            # ``FetchedValue`` marks a value the database supplies (BIGSERIAL, trigger
            # maintained); it carries no literal, so it is normalised to "database".
            default=_orm_default(column),
            computed=column.computed is not None,
            identity=column.identity is not None,
        )
    spec.primary_key = tuple(column.name for column in table.primary_key.columns)
    spec.foreign_keys = tuple(
        sorted(
            (
                ForeignKey(
                    columns=tuple(element.parent.name for element in constraint.elements),
                    referred_table=next(iter(constraint.elements)).column.table.name,
                    referred_columns=tuple(element.column.name for element in constraint.elements),
                    ondelete=constraint.ondelete,
                )
                for constraint in table.foreign_key_constraints
            ),
            key=lambda fk: (fk.columns, fk.referred_table),
        )
    )
    unique_sets: set[frozenset[str]] = set()
    for constraint in table.constraints:
        from sqlalchemy import UniqueConstraint

        if isinstance(constraint, UniqueConstraint):
            unique_sets.add(frozenset(column.name for column in constraint.columns))
    for index in table.indexes:
        if index.unique:
            unique_sets.add(frozenset(column.name for column in index.columns))
    spec.unique_sets = tuple(sorted(unique_sets, key=lambda columns: sorted(columns)))
    return spec


def orm_spec() -> DatabaseSpec:
    from app.models import Base

    spec = DatabaseSpec()
    for table in Base.metadata.sorted_tables:
        spec.tables[table.name] = _metadata_table(table)
    return spec


# ---------------------------------------------------------------------- database
def reflect_spec(engine: Engine, *, with_objects: bool = False) -> DatabaseSpec:
    """Reflect the structure of ``engine``'s ``public`` schema."""
    inspector = inspect(engine)
    spec = DatabaseSpec()

    for table_name in inspector.get_table_names(schema="public"):
        if table_name in TOOL_TABLES:
            continue
        table_spec = TableSpec(name=table_name)
        for column in inspector.get_columns(table_name, schema="public"):
            table_spec.columns[column["name"]] = Column(
                name=column["name"],
                type=canonical_type(column["type"]),
                nullable=bool(column["nullable"]),
                default=normalize_default(column.get("default")),
                computed=column.get("computed") is not None,
                identity=column.get("identity") is not None,
            )
        primary_key = inspector.get_pk_constraint(table_name, schema="public")
        table_spec.primary_key = tuple(primary_key.get("constrained_columns") or ())
        table_spec.foreign_keys = tuple(
            sorted(
                (
                    ForeignKey(
                        columns=tuple(fk["constrained_columns"]),
                        referred_table=fk["referred_table"],
                        referred_columns=tuple(fk["referred_columns"]),
                        ondelete=(fk.get("options") or {}).get("ondelete"),
                    )
                    for fk in inspector.get_foreign_keys(table_name, schema="public")
                ),
                key=lambda fk: (fk.columns, fk.referred_table),
            )
        )
        # Expression-based unique indexes reflect with a NULL column name; they are
        # compared by the DB-to-DB gate (predicate and expression included), not here.
        unique_sets: set[frozenset[str]] = set()
        for constraint in inspector.get_unique_constraints(table_name, schema="public"):
            constraint_columns = list(constraint.get("column_names") or [])
            # Every member must be a real column: an expression member reflects as None,
            # and treating the remaining columns as a unique set would invent a
            # constraint the database does not have.
            if constraint_columns and all(name is not None for name in constraint_columns):
                unique_sets.add(frozenset(str(name) for name in constraint_columns))
        for index in inspector.get_indexes(table_name, schema="public"):
            index_columns = list(index.get("column_names") or [])
            if (
                index.get("unique")
                and index_columns
                and all(name is not None for name in index_columns)
            ):
                unique_sets.add(frozenset(str(name) for name in index_columns))
        table_spec.unique_sets = tuple(sorted(unique_sets, key=lambda columns: sorted(columns)))
        spec.tables[table_name] = table_spec

        if with_objects:
            for index in inspector.get_indexes(table_name, schema="public"):
                spec.indexes[f"{table_name}.{index['name']}"] = IndexSpec(
                    name=str(index["name"]),
                    table=table_name,
                    columns=tuple(index.get("column_names") or ()),
                    unique=bool(index.get("unique")),
                    predicate=normalize_sql(
                        index.get("dialect_options", {}).get("postgresql_where")
                    ),
                )
            for check in inspector.get_check_constraints(table_name, schema="public"):
                spec.checks[f"{table_name}.{check['name']}"] = normalize_sql(check.get("sqltext"))

    if with_objects:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT c.relname AS table_name, t.tgname AS trigger_name
                    FROM pg_trigger t
                    JOIN pg_class c ON c.oid = t.tgrelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE NOT t.tgisinternal AND n.nspname = 'public'
                    """
                )
            ).all()
            for table_name, trigger_name in rows:
                spec.triggers[f"{table_name}.{trigger_name}"] = table_name

            rows = connection.execute(
                text(
                    """
                    SELECT p.proname,
                           pg_get_function_identity_arguments(p.oid) AS arguments
                    FROM pg_proc p
                    JOIN pg_namespace n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'public'
                    """
                )
            ).all()
            spec.functions = {
                f"{name}({arguments})" for name, arguments in rows if arguments is not None
            }

            rows = connection.execute(
                text(
                    """
                    SELECT c.relname AS view_name, pg_get_viewdef(c.oid, true) AS definition
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE c.relkind = 'v' AND n.nspname = 'public'
                    ORDER BY c.relname
                    """
                )
            ).all()
            spec.views = {name: normalize_sql(definition) for name, definition in rows}

    return spec


# --------------------------------------------------------------------------- diff
def _compare_columns(
    left_name: str, right_name: str, left: TableSpec, right: TableSpec
) -> list[str]:
    problems: list[str] = []
    for name in sorted(set(left.columns) - set(right.columns)):
        problems.append(f"MISSING_COLUMN  {left_name}.{name} (absent from {right_name})")
    for name in sorted(set(right.columns) - set(left.columns)):
        problems.append(f"EXTRA_COLUMN    {right_name}.{name} (absent from {left_name})")
    for name in sorted(set(left.columns) & set(right.columns)):
        left_column, right_column = left.columns[name], right.columns[name]
        if left_column.type != right_column.type:
            problems.append(
                f"TYPE            {left_name}.{name}: {left_column.type} != "
                f"{right_name}: {right_column.type}"
            )
        if left_column.nullable != right_column.nullable:
            problems.append(
                f"NULLABLE        {left_name}.{name}: "
                f"{left_column.nullable} != {right_column.nullable}"
            )
        if left_column.computed != right_column.computed:
            problems.append(
                f"GENERATED       {left_name}.{name}: generated={left_column.computed} != "
                f"generated={right_column.computed}"
            )
        if left_column.identity != right_column.identity:
            problems.append(f"IDENTITY        {left_name}.{name}: identity differs")
        # A default declared on the left that the right side does not have would make
        # the application believe in a value the database never produces.
        left_default = None if left_column.default == "database-supplied" else left_column.default
        if left_default and not right_column.default:
            problems.append(
                f"DEFAULT         {left_name}.{name}: '{left_default}' not present in {right_name}"
            )
        elif left_default and right_column.default and left_default != right_column.default:
            problems.append(
                f"DEFAULT         {left_name}.{name}: '{left_default}' != '{right_column.default}'"
            )
    return problems


def diff(left: DatabaseSpec, right: DatabaseSpec, *, left_name: str, right_name: str) -> list[str]:
    """Differences between two structures, phrased as ``left`` missing/diverging."""
    problems: list[str] = []
    for table in sorted(set(left.tables) - set(right.tables)):
        problems.append(f"MISSING_TABLE   {left_name}.{table} (absent from {right_name})")
    for table in sorted(set(right.tables) - set(left.tables)):
        problems.append(f"EXTRA_TABLE     {right_name}.{table} (absent from {left_name})")

    for table in sorted(set(left.tables) & set(right.tables)):
        left_table, right_table = left.tables[table], right.tables[table]
        problems.extend(_compare_columns(left_name, right_name, left_table, right_table))
        if set(left_table.primary_key) != set(right_table.primary_key):
            problems.append(
                f"PRIMARY_KEY     {table}: {left_table.primary_key} != {right_table.primary_key}"
            )
        left_fks = {
            (fk.columns, fk.referred_table, fk.referred_columns, fk.ondelete)
            for fk in left_table.foreign_keys
        }
        right_fks = {
            (fk.columns, fk.referred_table, fk.referred_columns, fk.ondelete)
            for fk in right_table.foreign_keys
        }
        for fk in sorted(left_fks - right_fks, key=str):
            problems.append(f"FOREIGN_KEY     {left_name}.{table}: {fk} not in {right_name}")
        for fk in sorted(right_fks - left_fks, key=str):
            problems.append(f"FOREIGN_KEY     {right_name}.{table}: {fk} not in {left_name}")
        left_uniques = {frozenset(value) for value in left_table.unique_sets}
        right_uniques = {frozenset(value) for value in right_table.unique_sets}
        for unique in sorted(left_uniques - right_uniques, key=lambda value: sorted(value)):
            problems.append(
                f"UNIQUE          {table}: ({', '.join(sorted(unique))}) not unique in {right_name}"
            )
    return problems


def diff_objects(left: DatabaseSpec, right: DatabaseSpec) -> list[str]:
    """Structural objects the migration must reproduce from the frozen DDL."""
    problems: list[str] = []
    for name in sorted(set(left.indexes) - set(right.indexes)):
        problems.append(f"MISSING_INDEX   {name}")
    for name in sorted(set(right.indexes) - set(left.indexes)):
        problems.append(f"EXTRA_INDEX     {name}")
    for name in sorted(set(left.indexes) & set(right.indexes)):
        left_index, right_index = left.indexes[name], right.indexes[name]
        if left_index.columns != right_index.columns:
            problems.append(
                f"INDEX_COLUMNS   {name}: {left_index.columns} != {right_index.columns}"
            )
        if left_index.unique != right_index.unique:
            problems.append(f"INDEX_UNIQUE    {name}: uniqueness differs")
        if left_index.predicate != right_index.predicate:
            problems.append(f"INDEX_PREDICATE {name}: predicate differs")
    for name in sorted(set(left.checks) - set(right.checks)):
        problems.append(f"MISSING_CHECK   {name}")
    for name in sorted(set(right.checks) - set(left.checks)):
        problems.append(f"EXTRA_CHECK     {name}")
    for name in sorted(set(left.checks) & set(right.checks)):
        if left.checks[name] != right.checks[name]:
            problems.append(f"CHECK_DEFINITION {name}")
    for name in sorted(set(left.triggers) - set(right.triggers)):
        problems.append(f"MISSING_TRIGGER {name}")
    for name in sorted(set(right.triggers) - set(left.triggers)):
        problems.append(f"EXTRA_TRIGGER   {name}")
    for name in sorted(left.functions - right.functions):
        problems.append(f"MISSING_ROUTINE {name}")
    for name in sorted(right.functions - left.functions):
        problems.append(f"EXTRA_ROUTINE   {name}")
    for name in sorted(set(left.views) - set(right.views)):
        problems.append(f"MISSING_VIEW    {name}")
    for name in sorted(set(right.views) - set(left.views)):
        problems.append(f"EXTRA_VIEW      {name}")
    for name in sorted(set(left.views) & set(right.views)):
        if left.views[name] != right.views[name]:
            problems.append(f"VIEW_DEFINITION {name}")
    return problems


# --------------------------------------------------------------------------- CLI
def _engine(dsn: str) -> Engine:
    return create_engine(dsn, future=True)


def _report(title: str, problems: list[str], *, verbose: bool, counts: dict[str, int]) -> int:
    print(title)
    for name, count in counts.items():
        print(f"  {name}: {count}")
    if not problems:
        print("  result: MATCH")
        return 0
    print(f"  result: MISMATCH ({len(problems)} difference(s))")
    shown = problems if verbose else problems[:40]
    for problem in shown:
        print(f"    {problem}")
    if not verbose and len(problems) > len(shown):
        print(f"    ... {len(problems) - len(shown)} more (run with --verbose)")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="schema_gate", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    orm_parser = subparsers.add_parser("orm-db", help="SQLAlchemy metadata vs database")
    orm_parser.add_argument("--dsn", help="database DSN (defaults to DATABASE_MIGRATION_URL)")
    orm_parser.add_argument("--verbose", action="store_true")

    db_parser = subparsers.add_parser("db-db", help="reference DDL vs migration result")
    db_parser.add_argument("--left", required=True, help="DSN built from schema.sql")
    db_parser.add_argument("--right", required=True, help="DSN built by the migration")
    db_parser.add_argument("--verbose", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "orm-db":
        from app.core.config import get_settings

        dsn = args.dsn or get_settings().migration_dsn_psycopg
        engine = _engine(dsn)
        try:
            database = reflect_spec(engine)
        finally:
            engine.dispose()
        models = orm_spec()
        problems = diff(models, database, left_name="ORM", right_name="DATABASE")
        return _report(
            "ORM vs DATABASE",
            problems,
            verbose=args.verbose,
            counts={
                "tables": len(models.tables),
                "columns": sum(len(table.columns) for table in models.tables.values()),
            },
        )

    left_engine, right_engine = _engine(args.left), _engine(args.right)
    try:
        left = reflect_spec(left_engine, with_objects=True)
        right = reflect_spec(right_engine, with_objects=True)
    finally:
        left_engine.dispose()
        right_engine.dispose()
    problems = diff(left, right, left_name="REFERENCE", right_name="MIGRATION")
    problems.extend(diff_objects(left, right))
    return _report(
        "REFERENCE (schema.sql) vs MIGRATION (alembic upgrade head)",
        problems,
        verbose=args.verbose,
        counts={
            "tables": len(left.tables),
            "indexes": len(left.indexes),
            "checks": len(left.checks),
            "triggers": len(left.triggers),
            "routines": len(left.functions),
            "views": len(left.views),
        },
    )


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
