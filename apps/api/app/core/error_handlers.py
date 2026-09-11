"""Exception handlers that guarantee the PART 38 error envelope.

Every failure path — domain error, request validation, HTTP error, or an
unexpected defect — leaves the API in exactly one shape:

``{"error": {"code", "message", "details"}, "request_id": "..."}``

Unexpected exceptions are logged with their traceback server-side and returned as
a generic ``INTERNAL_ERROR`` with the correlation id: no stack trace, SQL fragment
or internal path is ever exposed to a client (SECURITY.md §6).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import (
    ErrorCode,
    NexusError,
    ServiceUnavailableError,
    ValidationError,
    error_for_sqlstate,
)
from app.core.logging import get_logger, request_id_var

logger = get_logger(__name__)

_STATUS_FALLBACK_CODES: dict[int, ErrorCode] = {
    400: ErrorCode.VALIDATION_ERROR,
    401: ErrorCode.TOKEN_INVALID,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.RESOURCE_NOT_FOUND,
    405: ErrorCode.VALIDATION_ERROR,
    409: ErrorCode.DUPLICATE_RESOURCE,
    410: ErrorCode.CURSOR_EXPIRED,
    415: ErrorCode.VALIDATION_ERROR,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}


def _envelope(
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    status_code: int,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "error": {"code": code, "message": message, "details": details or {}}
    }
    request_id = request_id_var.get()
    if request_id:
        payload["request_id"] = request_id
    return JSONResponse(status_code=status_code, content=payload, headers=headers or None)


def _constraint_name(exc: IntegrityError) -> str | None:
    """Name of the violated constraint, when the driver exposes it.

    psycopg exposes ``exc.orig.diag``; SQLAlchemy does not type ``orig``, so the
    attribute is reached defensively and reported as unknown when absent.
    """
    original = getattr(exc, "orig", None)
    diag = getattr(original, "diag", None)
    return getattr(diag, "constraint_name", None)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the envelope handlers to the application."""

    @app.exception_handler(NexusError)
    async def _nexus_error(_request: Request, exc: NexusError) -> JSONResponse:
        if exc.http_status >= 500:
            logger.error(
                "domain_error",
                code=str(exc.code),
                message=exc.message,
                details=exc.details,
                exc_info=exc.http_status == 500,
            )
        else:
            logger.info("domain_error", code=str(exc.code), message=exc.message)
        return _envelope(
            code=str(exc.code),
            message=exc.message,
            details=exc.details,
            status_code=exc.http_status,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ()) if part != "body"),
                "code": error.get("type", "invalid"),
                "message": error.get("msg", "invalid value"),
            }
            for error in exc.errors()
        ]
        error = ValidationError("The request could not be validated.", details={"fields": fields})
        return _envelope(
            code=str(error.code),
            message=error.message,
            details=error.details,
            status_code=error.http_status,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_FALLBACK_CODES.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        response = _envelope(code=str(code), message=detail, status_code=exc.status_code)
        if exc.headers:
            for header, value in exc.headers.items():
                response.headers.setdefault(header, value)
        return response

    @app.exception_handler(IntegrityError)
    async def _integrity_error(_request: Request, exc: IntegrityError) -> JSONResponse:
        sqlstate = _sqlstate_of(exc)
        domain_error = error_for_sqlstate(
            sqlstate, "The request violates a database integrity rule."
        )
        logger.warning(
            "database_integrity_error",
            sqlstate=sqlstate,
            constraint=_constraint_name(exc),
        )
        return _envelope(
            code=str(domain_error.code),
            message=domain_error.message,
            details=domain_error.details,
            status_code=domain_error.http_status,
        )

    @app.exception_handler(OperationalError)
    async def _operational_error(_request: Request, exc: OperationalError) -> JSONResponse:
        logger.error("database_unavailable", error=str(exc))
        unavailable = ServiceUnavailableError("The database is temporarily unavailable.")
        return _envelope(
            code=str(unavailable.code),
            message=unavailable.message,
            status_code=unavailable.http_status,
        )

    @app.exception_handler(DBAPIError)
    async def _dbapi_error(_request: Request, exc: DBAPIError) -> JSONResponse:
        sqlstate = _sqlstate_of(exc)
        domain_error = error_for_sqlstate(sqlstate, "The request could not be completed.")
        logger.warning("database_error", sqlstate=sqlstate, error=str(exc))
        return _envelope(
            code=str(domain_error.code),
            message=domain_error.message,
            details=domain_error.details,
            status_code=domain_error.http_status,
        )

    @app.exception_handler(Exception)
    async def _unhandled_error(_request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_error", error_type=type(exc).__name__, exc_info=exc)
        return _envelope(
            code=str(ErrorCode.INTERNAL_ERROR),
            message="An unexpected error occurred. Quote the request id when reporting it.",
            status_code=500,
        )


def _sqlstate_of(exc: Exception) -> str | None:
    """Extract the PostgreSQL SQLSTATE from a SQLAlchemy exception, if present."""
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return str(sqlstate) if sqlstate else None
