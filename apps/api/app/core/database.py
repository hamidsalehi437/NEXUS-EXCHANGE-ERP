"""Database access: async engine, session/transaction management, health probes.

Design rules (ARCHITECTURE.md §5):

* Only services open/commit transactions. :meth:`Database.transaction` is the one
  place that commits — repositories receive a session and never commit.
* The async engine is used by the API and the worker; migrations and the seed
  runner use a **synchronous** psycopg engine (:func:`create_sync_engine`) because
  they execute reviewed SQL and need the simple-query protocol.
* :meth:`Database.ping` and :meth:`Database.schema_revision` back the readiness
  endpoint; a failing database is reported, never swallowed.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.exceptions import ServiceUnavailableError
from app.core.logging import get_logger
from app.models.base import Base

logger = get_logger(__name__)


def create_sync_engine(settings: Settings, *, purpose: str) -> Any:
    """Create a synchronous psycopg engine (migrations, seeds, CLI tooling)."""
    return create_engine(
        settings.migration_dsn_psycopg,
        pool_pre_ping=True,
        future=True,
        connect_args={"application_name": f"nexus-{purpose}"},
    )


def create_sync_session_factory(engine: Any) -> sessionmaker[Session]:
    """Session factory bound to a synchronous engine."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Database:
    """Async database handle with health checks and transaction helpers."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            pool_recycle=1800,
            # asyncpg takes these through the server's startup parameters, not as
            # connect() keyword arguments: ``application_name`` belongs inside
            # ``server_settings`` alongside the session guards the ledger relies on
            # (statement timeouts and a UTC session, so no timestamp is ever written
            # in a local zone).
            connect_args={
                "server_settings": {
                    "application_name": "nexus-api",
                    "statement_timeout": str(settings.db_statement_timeout_ms),
                    "idle_in_transaction_session_timeout": str(
                        settings.db_idle_in_transaction_timeout_ms
                    ),
                    "timezone": "UTC",
                },
            },
        )
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    # ------------------------------------------------------------- lifecycle
    async def connect_with_retry(self) -> None:
        """Wait for the database to accept connections, then return.

        Used at startup so a container that races its database fails *visibly*
        after a bounded number of attempts instead of serving broken requests.
        """
        attempts = self._settings.db_connect_retries
        delay = self._settings.db_connect_retry_delay_seconds
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                await self.ping()
                logger.info("database_connected", attempt=attempt)
                return
            except Exception as exc:
                last_error = exc
                logger.warning("database_connect_retry", attempt=attempt, error=str(exc))
                if attempt < attempts:
                    await asyncio.sleep(delay)

        raise ServiceUnavailableError(
            "Database is unreachable after startup retries.",
            details={"attempts": attempts, "error": str(last_error)},
        )

    async def dispose(self) -> None:
        await self._engine.dispose()

    # ----------------------------------------------------------------- health
    async def ping(self) -> None:
        """Raise when the database cannot execute a trivial query."""
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def schema_revision(self) -> str | None:
        """Return the applied Alembic revision (``None`` when unmigrated)."""
        async with self._engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT version_num FROM alembic_version"
                    if await self._has_alembic_table(connection)
                    else "SELECT NULL::text"
                )
            )
            return result.scalar_one_or_none()

    @staticmethod
    async def _has_alembic_table(connection: Any) -> bool:
        result = await connection.execute(text("SELECT to_regclass('public.alembic_version')"))
        return result.scalar_one_or_none() is not None

    async def server_version(self) -> str:
        async with self._engine.connect() as connection:
            result = await connection.execute(text("SHOW server_version"))
            return str(result.scalar_one())

    # --------------------------------------------------------------- sessions
    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session without an implicit commit (read paths, tests)."""
        session = self._session_factory()
        try:
            yield session
        finally:
            await session.close()

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """A session whose work is committed on success and rolled back on error.

        This is the boundary that makes PART 20 possible: the business document,
        its journal entry, its cash movement and its audit row are written in one
        database transaction — or not at all.
        """
        session = self._session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


def metadata_tables() -> list[str]:
    """Table names known to the ORM (used by tests and diagnostics)."""
    return sorted(Base.metadata.tables)


@contextlib.contextmanager
def sync_session(engine: Any) -> Iterator[Session]:
    """Synchronous session context manager for CLI tooling."""
    factory = create_sync_session_factory(engine)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
