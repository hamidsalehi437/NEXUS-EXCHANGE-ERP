"""Initial NEXUS EXCHANGE ERP schema (Phase 0 reference DDL).

Revision ID: 0001_initial_schema
Revises:
Created: 2026-09-11 14:00:00 UTC

This migration does not describe the schema in Alembic operations. It executes the
**frozen, approved DDL** (``alembic/sql/0001_initial_schema.sql``, a byte-for-byte copy
of ``docs/database/schema.sql``) inside the migration transaction, after verifying its
SHA-256 checksum against ``alembic/sql/CHECKSUMS.txt``.

Why a frozen file rather than ``op.create_table(...)``: the schema carries the financial
invariants themselves — deferred balance checks, immutability triggers, the audit hash
chain, the balance-cache trigger, partial/expression indexes and the money-type
self-check. Re-expressing all of that as Alembic operations would create a second,
reviewable-in-isolation definition of the accounting model. Applying the approved file
verbatim means there is exactly one definition, and the migration gate
(``scripts/schema_gate.py db-db``) proves that what the migration creates is structurally
identical to what the reference file creates.

If the checksum does not match, this migration raises and the upgrade aborts without
touching the database.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from alembic import op
from sqlalchemy import text
from sqlalchemy.engine import Connection

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
SCHEMA_FILE = SQL_DIR / "0001_initial_schema.sql"
EXPECTED_SHA256 = "37f7bc3cfc523589f934494b910a49db67a4d66cfd1b34b843cf3819102ae455"

# Dropped on downgrade, in dependency order. Views first, then tables (CASCADE removes
# their indexes and triggers), then the functions those objects used.
_VIEWS = (
    "v_exchange_commissions",
    "v_currency_exposure",
    "v_trial_balance",
    "v_recent_audit",
    "v_open_cash_sessions",
)

_TABLES = (
    "idempotency_keys",
    "sync_conflicts",
    "sync_events",
    "sync_cursors",
    "device_allocations",
    "allocation_policies",
    "change_log",
    "audit_logs",
    "refresh_tokens",
    "user_permissions",
    "user_roles",
    "role_permissions",
    "permissions",
    "roles",
    "users",
    "devices",
    "branches",
    "cash_movements",
    "cash_session_lines",
    "cash_sessions",
    "expenses",
    "transfers",
    "exchange_transactions",
    "exchange_rates",
    "journal_lines",
    "journal_entries",
    "account_balances",
    "accounts",
    "customers",
    "currencies",
    "sequences",
)

_FUNCTIONS = (
    "available_allocation(uuid, uuid, timestamp with time zone)",
    "nexus_assert_journal_balanced()",
    "nexus_assert_non_negative_cash()",
    "nexus_assert_reversal_bound()",
    "nexus_audit_chain()",
    "nexus_bump_version()",
    "nexus_currencies_immutable_code()",
    "nexus_forbid_mutation()",
    "nexus_journal_entries_immutable()",
    "nexus_log_change()",
    "nexus_maintain_account_balances()",
    "nexus_set_updated_at()",
    "nexus_validate_cash_session_line()",
    "nexus_validate_exchange_reversal()",
    "nexus_validate_exchange_status()",
    "nexus_validate_transfer_status()",
    "rebuild_account_balances()",
    "resolve_exchange_rate(uuid, uuid, timestamp with time zone)",
    "next_document_number(character varying, uuid)",
    "verify_audit_chain(bigint)",
)


def _read_schema() -> str:
    """Return the frozen DDL, refusing to continue if it is not the approved file."""
    ddl = SCHEMA_FILE.read_text(encoding="utf-8")
    digest = hashlib.sha256(ddl.encode("utf-8")).hexdigest()
    if digest != EXPECTED_SHA256:
        raise RuntimeError(
            "Refusing to migrate: the initial schema file does not match the approved "
            f"checksum.\n  file:     {SCHEMA_FILE}\n  expected: {EXPECTED_SHA256}\n"
            f"  actual:   {digest}\n"
            "The approved DDL must be applied verbatim (docs/database/SCHEMA.md §10)."
        )
    return ddl


def _execute_script(connection: Connection, statements: str) -> None:
    """Execute a multi-statement script through the raw DBAPI connection.

    psycopg3 only accepts multiple statements when no parameters are bound (simple-query
    protocol), and SQLAlchemy always binds an empty parameter set. Going one level down
    to the driver keeps the script identical to the reviewed file — including its
    dollar-quoted function bodies and the ``%`` characters inside RAISE messages.
    """
    driver_connection = connection.connection.driver_connection  # type: ignore[attr-defined]
    with driver_connection.cursor() as cursor:
        cursor.execute(statements)


def upgrade() -> None:
    ddl = _read_schema()
    # Defensive assertion: the file must contain the money-type self-check that makes a
    # float column impossible to introduce unnoticed.
    if "NUMERIC(30,10)" not in ddl or "NEXUS_SCHEMA_NO_MONEY_COLUMNS" not in ddl:
        raise RuntimeError("The initial schema is missing its money-type self-check.")

    connection = op.get_bind()
    _execute_script(connection, ddl)


def downgrade() -> None:
    """Drop everything this revision created (test databases only).

    Production rollback is by restore, never by downgrade (docs/database/SCHEMA.md §10).
    Audit-log and ledger *rows* are dropped with their tables here because this is a
    full-schema teardown, not a retention operation; the application never deletes them.
    """
    connection = op.get_bind()

    for view in _VIEWS:
        connection.execute(text(f"DROP VIEW IF EXISTS {view} CASCADE"))
    for table in _TABLES:
        connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    for function in _FUNCTIONS:
        connection.execute(text(f"DROP FUNCTION IF EXISTS {function} CASCADE"))
