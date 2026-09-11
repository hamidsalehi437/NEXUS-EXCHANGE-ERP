"""Application factory, lifespan and middleware stack.

Startup order is deliberate: settings are validated first (fail fast on a missing
secret), logging is configured, then the database and Redis handles are created.
The database is awaited with a bounded retry so the container reports a clear
error instead of serving requests it cannot fulfil; Redis is best-effort because
it holds no financial state (ADR-002) and the readiness probe reports it.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.database import Database
from app.core.error_handlers import register_exception_handlers
from app.core.logging import REQUEST_ID_HEADER, configure_logging, get_logger, request_id_var
from app.core.redis import RedisManager

logger = get_logger(__name__)

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; sandbox",
    "Cache-Control": "no-store",
}

HSTS_HEADER = ("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign/propagate a request id, log the access line and add security headers."""

    def __init__(self, app: ASGIApp, *, secure_headers: bool, force_https: bool) -> None:
        super().__init__(app)
        self._secure_headers = secure_headers
        self._force_https = force_https

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id

        token = request_id_var.set(request_id)
        begin = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - begin) * 1000, 2)
            logger.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            raise
        finally:
            request_id_var.reset(token)

        duration_ms = round((time.perf_counter() - begin) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id

        if self._secure_headers:
            for header, value in SECURITY_HEADERS.items():
                response.headers.setdefault(header, value)
            if self._force_https or request.url.scheme == "https":
                response.headers.setdefault(*HSTS_HEADER)

        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            request_id=request_id,
        )
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application (used by uvicorn and by the test suite)."""
    app_settings = settings or get_settings()
    configure_logging(level=app_settings.log_level, fmt=app_settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(app_settings)
        redis = RedisManager(app_settings)
        app.state.database = database
        app.state.redis = redis
        app.state.settings = app_settings

        logger.info("startup_begin", **app_settings.safe_summary)

        await database.connect_with_retry()

        try:
            await redis.ping()
            logger.info("redis_connected")
        except Exception as exc:
            logger.warning("redis_unavailable_at_startup", error=str(exc))

        try:
            yield
        finally:
            logger.info("shutdown_begin")
            await redis.close()
            await database.dispose()
            logger.info("shutdown_complete")

    app = FastAPI(
        title=app_settings.app_name,
        version=app_settings.app_version,
        description=(
            "NEXUS EXCHANGE ERP API — offline-first, multi-currency, multi-branch ERP for "
            "licensed currency-exchange and money-service businesses. Phase 1 exposes "
            "system endpoints only; feature routers are added as each phase is delivered."
        ),
        docs_url="/api/docs" if not app_settings.is_production else None,
        redoc_url="/api/redoc" if not app_settings.is_production else None,
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )

    if app_settings.trusted_hosts_list:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=app_settings.trusted_hosts_list)

    if app_settings.cors_origins_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=app_settings.cors_origins_list,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER, "Idempotency-Key"],
            expose_headers=[REQUEST_ID_HEADER],
            max_age=600,
        )

    app.add_middleware(
        RequestContextMiddleware,
        secure_headers=app_settings.secure_headers_enabled,
        force_https=app_settings.force_https,
    )

    register_exception_handlers(app)
    app.include_router(api_router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": app_settings.app_name,
            "version": app_settings.app_version,
            "environment": app_settings.app_env,
            "api": "/api/v1",
            "health": "/api/v1/health",
            "readiness": "/api/v1/health/ready",
        }

    return app


app = create_app()
