"""Liveness, readiness and build metadata (PART 39, API contract §1).

* ``GET /api/v1/health`` — liveness. The process is up and the event loop responds.
  It deliberately does **not** touch PostgreSQL or Redis: a container orchestrator
  must not restart a healthy API because a dependency blipped.
* ``GET /api/v1/health/ready`` — readiness. Probes PostgreSQL (query + applied
  migration revision) and Redis, and reports ``503`` with per-component detail when
  something required is unusable. A device in the field uses this response to decide
  whether it may trade online or should fall back to its offline allowance.
* ``GET /api/v1/version`` — build metadata for operators and support tickets.
"""

from __future__ import annotations

import asyncio
import platform
import sys
import time

from fastapi import APIRouter, Response, status

from app.api.deps import OptionalDatabaseDep, OptionalRedisDep, SettingsDep
from app.schemas.common import ComponentHealth, HealthResponse, ReadinessResponse, VersionResponse

router = APIRouter(tags=["system"])

_READINESS_TIMEOUT_SECONDS = 3.0


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
    )


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe (PostgreSQL + Redis)",
    responses={503: {"description": "A required dependency is unavailable"}},
)
async def readiness(
    settings: SettingsDep,
    database: OptionalDatabaseDep,
    redis: OptionalRedisDep,
    response: Response,
) -> ReadinessResponse:
    components: list[ComponentHealth] = []
    schema_revision: str | None = None

    # --- PostgreSQL -----------------------------------------------------------
    started = time.perf_counter()
    try:
        if database is None:
            raise RuntimeError("database handle was not initialised during startup")
        revision = await asyncio.wait_for(
            database.schema_revision(), timeout=_READINESS_TIMEOUT_SECONDS
        )
        schema_revision = revision
        components.append(
            ComponentHealth(
                name="postgresql",
                status="ok",
                detail=f"schema revision {revision}" if revision else "no migration applied",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        )
    except Exception as exc:
        components.append(
            ComponentHealth(name="postgresql", status="unavailable", detail=type(exc).__name__)
        )

    # --- Redis ----------------------------------------------------------------
    started = time.perf_counter()
    try:
        if redis is None:
            raise RuntimeError("redis handle was not initialised during startup")
        await asyncio.wait_for(redis.ping(), timeout=_READINESS_TIMEOUT_SECONDS)
        components.append(
            ComponentHealth(
                name="redis",
                status="ok",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        )
    except Exception as exc:
        components.append(
            ComponentHealth(name="redis", status="unavailable", detail=type(exc).__name__)
        )

    ready = all(component.status == "ok" for component in components)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if ready else "degraded",
        environment=settings.app_env,
        components=components,
        schema_revision=schema_revision,
    )


@router.get("/version", response_model=VersionResponse, summary="Build metadata")
async def version(settings: SettingsDep, database: OptionalDatabaseDep) -> VersionResponse:
    try:
        revision = await database.schema_revision() if database is not None else None
    except Exception:
        revision = None

    return VersionResponse(
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        git_sha=settings.git_sha,
        build_time=settings.build_time,
        python_version=platform.python_version(),
        schema_revision=revision,
    )


@router.get("/health/runtime", summary="Runtime details for support")
async def runtime_info(database: OptionalDatabaseDep) -> dict[str, object]:
    """Non-secret runtime facts: interpreter, driver and PostgreSQL versions."""
    postgres_version: str | None
    try:
        postgres_version = await database.server_version() if database is not None else None
    except Exception:
        postgres_version = None

    return {
        "python_version": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "postgresql_version": postgres_version,
    }
