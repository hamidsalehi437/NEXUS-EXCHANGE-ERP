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
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.auth_helpers import USER_PASSWORD
from tests.helpers import (
    DEV_ADMIN_PASSWORD,
    TEST_DATABASE_NAME,
    TEST_REDIS_DB,
    admin_engine,
    create_database,
    database_dsn,
    drop_database,
    fetch_scalar,
    migrate,
    redis_url,
    run_seeds,
)
from tests.helpers import (
    create_user as create_user_row,
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
    """The migrated database, with the approved seed data applied.

    The seed run happens with ``APP_ENV=development`` and a development password, which is
    the *only* configuration in which seeds 004 and 005 create the bootstrap
    administrator and the ``MAIN`` branch. Phase 2 needs both: a device cannot register
    without a branch, and user administration needs a caller holding ``users.manage``.

    That environment applies to the seed child process alone. The API under test still
    runs with ``APP_ENV=test`` (set for the test process above), so the development-only
    behaviour of the application itself is unchanged — and a deployment seeded in
    production never gets either row (see ``seeds/004_dev_admin.py``).
    """
    result = run_seeds(
        migrated_database,
        extra_env={"APP_ENV": "development", "DEV_ADMIN_PASSWORD": DEV_ADMIN_PASSWORD},
    )
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


@pytest.fixture(autouse=True)
def clear_rate_limits() -> Iterator[None]:
    """Reset the API's Redis state (rate-limit counters, token denylist) around a test.

    The limiter is part of the system under test, so its counters must not leak from one
    test to the next: a bucket left behind by an earlier case would turn an unrelated
    login into a ``429``. Only the ``nexus:`` namespace of the test database (15) is
    touched, and the flush happens whether or not a test asks for the fixture by name.

    Jobs that do not run a Redis service (unit tests, the static infrastructure checks,
    the OpenAPI export) must still be able to run their tests, so an unreachable Redis is
    tolerated *unless* the suite was pointed at one explicitly through
    ``NEXUS_TEST_REDIS_URL`` — the integration job always sets it, and there a failure is
    a real failure that must surface instead of being swallowed.
    """

    import logging

    import redis as redis_sync

    explicit_url = os.environ.get("NEXUS_TEST_REDIS_URL")

    def _flush() -> None:
        client = redis_sync.from_url(
            redis_url(),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=5,
        )
        try:
            keys = list(client.scan_iter(match="nexus:*", count=500))
            if keys:
                client.delete(*keys)
        finally:
            client.close()

    def _reset() -> None:
        try:
            _flush()
        except redis_sync.RedisError:
            if explicit_url:
                raise
            logging.getLogger("tests").debug(
                "no Redis reachable; the rate-limit counters were not reset"
            )

    _reset()
    yield
    _reset()


@pytest.fixture(scope="session")
def branch_id(main_database: str) -> str:
    """Identifier of the bootstrap branch created by seed 005 (dev/test environments)."""
    value = fetch_scalar(main_database, "SELECT id FROM branches WHERE code = 'MAIN'")
    assert value is not None, "seed 005 did not create the bootstrap branch"
    return str(value)


@pytest.fixture(scope="session")
def admin_user_id(main_database: str) -> str:
    """The seeded development administrator (SUPER_ADMIN)."""
    value = fetch_scalar(main_database, "SELECT id FROM users WHERE username = 'admin'")
    assert value is not None, "seed 004 did not create the development administrator"
    return str(value)


@pytest.fixture
def make_user(main_database: str) -> object:
    """Factory creating a real user (Argon2id credentials + role rows) in the test DB."""

    def _make(
        *,
        roles: object = ("CASHIER",),
        password: str = USER_PASSWORD,
        is_active: bool = True,
        must_change_password: bool = False,
        username: str | None = None,
        email: str | None = None,
        locked_until: str | None = None,
    ) -> dict[str, object]:
        import uuid as _uuid

        name = username or f"user-{_uuid.uuid4().hex[:10]}"
        user_id = create_user_row(
            main_database,
            username=name,
            password=password,
            roles=tuple(roles) if roles else (),
            is_active=is_active,
            must_change_password=must_change_password,
            email=email,
            locked_until=locked_until,
        )
        return {"id": user_id, "username": name, "password": password}

    return _make


@pytest.fixture
def admin_tokens(api_client: TestClient) -> dict[str, Any]:
    """Log the seeded development administrator in (a fresh installation each time)."""
    from tests.auth_helpers import ADMIN_USERNAME, login

    return login(api_client, ADMIN_USERNAME, DEV_ADMIN_PASSWORD).json()


@pytest.fixture
def admin_headers(admin_tokens: dict[str, Any]) -> dict[str, str]:
    """Authorization headers for the development administrator."""
    from tests.auth_helpers import bearer

    return bearer(str(admin_tokens["access_token"]), str(admin_tokens["device"]["id"]))


@pytest.fixture
def provisioned_device(api_client: TestClient, admin_headers: dict[str, str], branch_id: str):
    """Factory provisioning a device through the API so a user can log in on it.

    Some roles (AUDITOR, ACCOUNTANT) deliberately lack ``device.register``: their
    installations are provisioned by an administrator, which is the documented process.
    """

    def _provision(platform: str = "WINDOWS") -> str:
        from tests.auth_helpers import register_device

        device = register_device(api_client, admin_headers, branch_id=branch_id, platform=platform)
        return str(device["device_uuid"])

    return _provision


@pytest.fixture(scope="session")
def limited_admin_role(main_database: str) -> str:
    """A non-seeded role that may administer users but nothing else.

    The seeded matrix has no such role (``users.manage`` comes with OWNER/SUPER_ADMIN,
    which hold every permission), so the escalation guard is tested against a role built
    for it. Creating it with SQL keeps the test focused and needs no product change: role
    management UI is Phase 3+ scope, and the catalogue itself is seeded.
    """
    from tests.helpers import execute_sql

    name = "HELPDESK"
    execute_sql(
        main_database,
        "INSERT INTO roles (name, description, is_system) VALUES (:name, :description, false) "
        "ON CONFLICT (name) DO NOTHING",
        name=name,
        description="Phase 2 test role: user administration only",
    )
    execute_sql(
        main_database,
        "INSERT INTO role_permissions (role_id, permission_code) "
        "SELECT id, 'users.manage' FROM roles WHERE name = :name "
        "ON CONFLICT DO NOTHING",
        name=name,
    )
    return name


@pytest.fixture(scope="session")
def test_redis_database() -> int:
    """The Redis database the suite is allowed to touch."""
    return TEST_REDIS_DB


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
