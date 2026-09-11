"""Alembic environment (PART 1, PART 44).

Migrations run as the **schema owner** (``DATABASE_MIGRATION_URL``), never as the
application runtime role: the runtime role has DML-only privileges by design
(docs/database/SCHEMA.md §8). The URL comes from validated settings, so a missing or
malformed value fails before any database work starts.

``alembic upgrade`` uses a synchronous psycopg connection. That is deliberate: the
frozen reference DDL is executed as one script through psycopg's simple-query protocol
(see ``versions/0001_initial_schema``), which is only available on the synchronous
driver.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

from app.core.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
settings = get_settings()

# Schema-change policy (docs/database/SCHEMA.md §10): the approved schema is applied as
# reviewed SQL, not generated from the models. Autogenerate is therefore not part of the
# workflow — a new revision is written by hand, reviewed, and validated by the migration
# gate (`scripts/schema_gate.py db-db`) plus the Phase 0 invariant suite.
# `target_metadata` is still wired up so `alembic check` can detect ORM/schema drift in
# CI and so a future phase can autogenerate against a reviewed baseline.


def _database_url() -> str:
    """Synchronous DSN for migrations, derived from validated settings."""
    return settings.migration_dsn_psycopg


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against the configured database."""
    connectable = create_engine(_database_url(), poolclass=pool.NullPool, future=True)

    with connectable.connect() as connection:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
