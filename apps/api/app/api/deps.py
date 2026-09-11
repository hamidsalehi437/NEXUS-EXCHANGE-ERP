"""FastAPI dependencies shared by the v1 routers.

Phase 1 provides the infrastructure dependencies (settings, database handle,
Redis handle, request id). Authentication and permission dependencies arrive in
Phase 2 and will be added here, so routers never reach into app state directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import Database
from app.core.exceptions import AuthenticationError, ServiceUnavailableError
from app.core.redis import RedisManager

# `auto_error=False` keeps this dependency optional until Phase 2 wires real
# token verification; the router-level dependencies decide what is required.
bearer_scheme = HTTPBearer(auto_error=False)


def get_app_settings() -> Settings:
    """Validated settings for the running process."""
    return get_settings()


def _state_handle(request: Request, name: str) -> object | None:
    """Return a lifespan-created handle, or ``None`` when startup did not finish."""
    return getattr(request.app.state, name, None)


def get_database(request: Request) -> Database:
    """The process-wide database handle created in the lifespan.

    A missing handle means the API is running without a usable database (startup
    failed or is still in progress). Answering ``503`` is correct: the caller must
    retry, and for a device in the field that means staying offline rather than
    trading against an unknown ledger state.
    """
    database = _state_handle(request, "database")
    if database is None:
        raise ServiceUnavailableError(
            "The database is not available.",
            details={
                "component": "postgresql",
                "hint": "Retry shortly; check /api/v1/health/ready.",
            },
        )
    return cast(Database, database)


def get_redis(request: Request) -> RedisManager:
    """The process-wide Redis handle created in the lifespan (see ``get_database``)."""
    redis = _state_handle(request, "redis")
    if redis is None:
        raise ServiceUnavailableError(
            "Redis is not available.",
            details={"component": "redis", "hint": "Retry shortly; check /api/v1/health/ready."},
        )
    return cast(RedisManager, redis)


def get_optional_database(request: Request) -> Database | None:
    """Database handle for probes that must answer even when it is unavailable."""
    return cast(Database | None, _state_handle(request, "database"))


def get_optional_redis(request: Request) -> RedisManager | None:
    """Redis handle for probes that must answer even when it is unavailable."""
    return cast(RedisManager | None, _state_handle(request, "redis"))


def get_request_id(request: Request) -> str | None:
    """Correlation id assigned by the request-id middleware."""
    return getattr(request.state, "request_id", None)


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    """Read-oriented session (no implicit commit; services open transactions)."""
    async with database.session() as session:
        yield session


def require_authentication(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> None:
    """Placeholder-free guard used by endpoints that will require a session.

    Phase 1 exposes no authenticated endpoint yet. This dependency exists so that
    routers built in the next phase start from a single, tested 401 path rather
    than each inventing one; it always rejects, which is the correct behaviour for
    an endpoint that has no authentication implementation behind it.
    """
    if credentials is None:
        raise AuthenticationError(
            "Authentication is required for this endpoint.",
            details={"hint": "Send an OAuth2 bearer token in the Authorization header."},
            http_status=status.HTTP_401_UNAUTHORIZED,
        )
    raise AuthenticationError(
        "Token verification is not enabled in this build stage.",
        details={"hint": "Authentication is implemented in Phase 2."},
        http_status=status.HTTP_401_UNAUTHORIZED,
    )


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]
RedisDep = Annotated[RedisManager, Depends(get_redis)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
# These two must use a real type (not a quoted forward reference): FastAPI reads the
# first argument of Annotated to decide whether a parameter is a dependency or a
# request field, and a string literal makes it fall back to a query parameter.
OptionalDatabaseDep = Annotated[Database | None, Depends(get_optional_database)]
OptionalRedisDep = Annotated[RedisManager | None, Depends(get_optional_redis)]
RequestIdDep = Annotated[str | None, Depends(get_request_id)]
