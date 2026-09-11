"""Helpers shared by the NEXUS test suite.

Everything here works against real PostgreSQL: the suite creates throwaway databases,
migrates them with the real Alembic command, and inspects the result. There is no
in-memory substitute for the database anywhere in this project — the schema *is* the set
of financial invariants (PART 49), so testing against a different engine would not test
the system.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

API_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = API_ROOT.parent.parent
# The approved schema, applied verbatim by the initial migration and by the db-db gate.
REFERENCE_SCHEMA = REPO_ROOT / "docs" / "database" / "schema.sql"

# The database and Redis database used by the default test session.
TEST_DATABASE_NAME = "nexus_test_main"
TEST_REDIS_DB = 15

# Password for the development administrator created by seed 004. Test-only value:
# it never reaches a deployment, and the seed refuses to run in production anyway.
DEV_ADMIN_PASSWORD = "Test-Admin-Password-2026!"

DEFAULT_ADMIN_DSN = "postgresql+psycopg://postgres@127.0.0.1:5432/postgres"


def admin_dsn() -> str:
    """DSN of a role allowed to create/drop databases (``NEXUS_TEST_ADMIN_DSN``)."""
    return os.environ.get("NEXUS_TEST_ADMIN_DSN", DEFAULT_ADMIN_DSN)


def database_dsn(database: str, *, driver: str = "psycopg") -> str:
    """Build a DSN for ``database`` by reusing the host/credentials of the admin DSN.

    The maintenance database in the admin DSN is *replaced*, not extended: building
    the string by hand produced ``host:5432/postgres/nexus_test_main``.
    """
    url = make_url(admin_dsn()).set(drivername=f"postgresql+{driver}", database=database)
    return url.render_as_string(hide_password=False)


def redis_url() -> str:
    """Redis URL for integration tests.

    ``NEXUS_TEST_REDIS_URL`` overrides the default (for example a server that
    requires a password, or the CI service container on another host). The tests
    use database 15 and never touch the application's databases 0-2.
    """
    return os.environ.get("NEXUS_TEST_REDIS_URL", f"redis://127.0.0.1:6379/{TEST_REDIS_DB}")


def admin_engine() -> Engine:
    """Engine connected to the maintenance database, outside any transaction."""
    return create_engine(admin_dsn(), isolation_level="AUTOCOMMIT", future=True)


def create_database(name: str) -> None:
    """Drop and recreate ``name`` so every run starts from an empty database."""
    engine = admin_engine()
    try:
        with engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        # Leaving a psycopg connection to the garbage collector raises a
        # ResourceWarning, and the suite treats warnings as errors.
        engine.dispose()


def drop_database(name: str) -> None:
    engine = admin_engine()
    try:
        with engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        engine.dispose()


def subprocess_environment(database: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a child process that must talk to ``database``.

    ``DATABASE_MIGRATION_URL`` keeps the **asyncpg** scheme on purpose: settings
    validate both DSNs the same way, and ``Settings.migration_dsn_psycopg`` performs
    the asyncpg -> psycopg translation for Alembic and the seed runner.
    """
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": database_dsn(database, driver="asyncpg"),
            "DATABASE_MIGRATION_URL": database_dsn(database, driver="asyncpg"),
        }
    )
    if extra:
        env.update(extra)
    return env


def run_alembic(
    database: str, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the real ``alembic`` command against ``database`` (the documented path)."""
    env = subprocess_environment(database, extra_env)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def migrate(database: str) -> None:
    """Bring ``database`` to ``head``; raise with the command output when it fails."""
    result = run_alembic(database, "upgrade", "head")
    if result.returncode != 0:
        raise AssertionError(
            f"alembic upgrade head failed for {database}:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def run_seeds(
    database: str, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m seeds`` against ``database``."""
    env = subprocess_environment(database, extra_env)
    return subprocess.run(
        [sys.executable, "-m", "seeds", *args],
        cwd=API_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def execute_sql_file(engine: Engine, path: Path) -> None:
    """Execute a multi-statement SQL file through psycopg's simple-query protocol.

    Used to apply the frozen reference DDL exactly as ``psql -f`` would, without
    depending on the ``psql`` binary being installed.
    """
    script = path.read_text(encoding="utf-8")
    # ``engine.begin()`` opens a real transaction: executing the script on the raw
    # driver connection behind SQLAlchemy's back means SQLAlchemy does not know a
    # transaction is in progress, and a plain ``connect()`` block would roll the
    # whole script back on close.
    with engine.begin() as connection:
        driver_connection = connection.connection.driver_connection  # type: ignore[attr-defined]
        with driver_connection.cursor() as cursor:
            cursor.execute(script)


def execute_statements(engine: Engine, statements: Iterable[str]) -> None:
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def fetch_all(database: str, sql: str, **params: object) -> list[tuple[object, ...]]:
    """Run a query against ``database`` and return its rows."""
    engine = create_engine(database_dsn(database), future=True)
    try:
        with engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(sql), params).all()]
    finally:
        engine.dispose()


def execute_sql(database: str, sql: str, **params: object) -> int:
    """Run a writing statement against ``database`` and return its row count.

    Fixtures and assertions that must set up (or repair) database state directly use
    this; the API itself is never reached this way.
    """
    engine = create_engine(database_dsn(database), future=True)
    try:
        with engine.begin() as connection:
            result = connection.execute(text(sql), params)
            return int(result.rowcount or 0)
    finally:
        engine.dispose()


def fetch_scalar(database: str, sql: str, **params: object) -> object:
    rows = fetch_all(database, sql, **params)
    return rows[0][0] if rows else None


def table_names(database: str) -> set[str]:
    rows = fetch_all(
        database,
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        """,
    )
    return {str(row[0]) for row in rows}


def seed_environment(database: str) -> dict[str, str]:
    """Environment for a process that must talk to ``database``."""
    return subprocess_environment(database)


def read_checksums_file() -> dict[str, str]:
    """Parse ``alembic/sql/CHECKSUMS.txt`` into ``{path: sha256}``."""
    checksums: dict[str, str] = {}
    path = API_ROOT / "alembic" / "sql" / "CHECKSUMS.txt"
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        digest, _, relative = stripped.partition("  ")
        checksums[relative.strip()] = digest.strip()
    return checksums


# --------------------------------------------------------------------------- Phase 2
def create_user(
    database: str,
    *,
    username: str,
    password: str,
    roles: Sequence[str] = (),
    is_active: bool = True,
    must_change_password: bool = False,
    email: str | None = None,
    full_name: str | None = None,
    locked_until: str | None = None,
) -> str:
    """Insert a user with real Argon2id credentials and role assignments.

    Fixture data is created directly so a test that is *about* login does not depend on
    user creation working; tests that exercise ``POST /users`` build their fixtures
    through the API instead.
    """
    from app.core.security import build_password_hasher

    hasher = build_password_hasher(time_cost=3, memory_cost=65_536, parallelism=4)
    engine = create_engine(database_dsn(database), future=True)
    try:
        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    INSERT INTO users (username, full_name, email, password_hash,
                                       is_active, must_change_password, locked_until)
                    VALUES (:username, :full_name, :email, :password_hash,
                            :is_active, :must_change_password, CAST(:locked_until AS timestamptz))
                    RETURNING id
                    """
                ),
                {
                    "username": username,
                    "full_name": full_name or f"Test {username}",
                    "email": email,
                    "password_hash": hasher.hash(password),
                    "is_active": is_active,
                    "must_change_password": must_change_password,
                    "locked_until": locked_until,
                },
            ).one()
            user_id = str(rows[0])
            for role in roles:
                connection.execute(
                    text(
                        "INSERT INTO user_roles (user_id, role_id) "
                        "SELECT :user_id, id FROM roles WHERE name = :role"
                    ),
                    {"user_id": user_id, "role": role},
                )
        return user_id
    finally:
        engine.dispose()


def git_tracked_files(*patterns: str) -> Sequence[str]:
    """List tracked files matching the patterns (used by 'no secrets committed' checks)."""
    result = subprocess.run(
        ["git", "ls-files", *patterns],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]
