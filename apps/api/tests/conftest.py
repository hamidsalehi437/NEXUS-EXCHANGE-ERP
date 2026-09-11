"""Pytest fixtures for the NEXUS EXCHANGE ERP test suite (PART 48).

Test isolation strategy:

* **One migrated database per session** (``nexus_test_main``), created empty, migrated
  with the real ``alembic upgrade head`` command and seeded with the real seed runner.
  The API under test talks to it through ``DATABASE_URL``.
* **Throwaway databases on demand** (``scratch_database``) for tests that need to
  control schema state themselves: the migration gate, the reference-schema comparison
  and the Phase 0 invariant suite.
* Environment variables are set here, at import time, *before* ``app.core.config`` is
  imported anywhere. Explicit environment variables take precedence over the developer's
  ``.env`` file in pydantic-settings, which keeps the suite independent of local values.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tests.helpers import (
    DEV_ADMIN_PASSWORD,
    TEST_DATABASE_NAME,
    admin_engine,
    create_database,
    database_dsn,
    drop_database,
    migrate,
    redis_url,
    run_seeds,
)

# --------------------------------------------------------------------------- environment
# Values good enough for a disposable test database; CI overrides them through the
# environment where needed (for example a different database host).
_TEST_DEFAULTS = {
    "APP_ENV": "test",
    "APP_NAME": "NEXUS EXCHANGE ERP",
    # Both DSNs use the async scheme: the settings model validates them uniformly and
    # converts the migration DSN to psycopg itself (Settings.migration_dsn_psycopg).
    "DATABASE_URL": database_dsn(TEST_DATABASE_NAME, driver="asyncpg"),
    "DATABASE_MIGRATION_URL": database_dsn(TEST_DATABASE_NAME, driver="asyncpg"),
    "REDIS_URL": redis_url(),
    "JWT_SECRET": "41" * 32,
    "JWT_REFRESH_SECRET": "42" * 32,
    "CORS_ORIGINS": "http://localhost:3000",
    "TRUSTED_HOSTS": "testserver,localhost,127.0.0.1",
    "DEV_ADMIN_USERNAME": "admin",
    "LOG_LEVEL": "WARNING",
    "LOG_FORMAT": "console",
    "STORAGE_PATH": "/tmp/nexus-test-storage",
}

for _key, _value in _TEST_DEFAULTS.items():
    os.environ.setdefault(_key, _value)


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[str]:
    """A database migrated to ``head`` with the real Alembic command."""
    create_database(TEST_DATABASE_NAME)
    migrate(TEST_DATABASE_NAME)
    yield TEST_DATABASE_NAME
    drop_database(TEST_DATABASE_NAME)


@pytest.fixture(scope="session")
def seeded_database(migrated_database: str) -> str:
    """The migrated database, with the approved seed data applied."""
    # The development administrator is seeded only when the password is supplied; it is
    # passed to the seed process rather than being ambient in every test process.
    result = run_seeds(migrated_database, extra_env={"DEV_ADMIN_PASSWORD": DEV_ADMIN_PASSWORD})
    if result.returncode != 0:
        raise AssertionError(f"seeding failed:\n{result.stdout}\n{result.stderr}")
    return migrated_database


@pytest.fixture(scope="session")
def main_database(seeded_database: str) -> str:
    """The database the API under test is pointed at."""
    return seeded_database


@pytest.fixture
def scratch_database() -> Iterator[object]:
    """Factory creating throwaway databases that are dropped at teardown."""
    created: list[str] = []

    def _make(name: str, *, do_migrate: bool = True) -> str:
        create_database(name)
        created.append(name)
        if do_migrate:
            migrate(name)
        return name

    yield _make

    for name in created:
        drop_database(name)


@pytest.fixture(scope="session")
def admin() -> Iterator[object]:
    """Autocommit engine on the maintenance database (CREATE/DROP DATABASE)."""
    engine = admin_engine()
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def api_client(main_database: str) -> Iterator[TestClient]:
    """A ``TestClient`` running the real application, lifespan included.

    The lifespan is what connects to PostgreSQL and Redis, so probes and error
    handlers are exercised exactly as in production.
    """
    from app.main import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client
