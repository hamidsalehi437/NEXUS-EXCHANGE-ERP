"""Shared response schemas: error envelope, pagination, health and version.

The error envelope is the contract every endpoint honours (PART 38). Money never
appears in these models yet — Phase 1 exposes operational endpoints only, and the
monetary schemas arrive with the features that use them (never before).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    """The ``error`` object of every non-2xx response."""

    code: str = Field(description="Stable machine-readable error code")
    message: str = Field(description="Human-readable message; the client localises by code")
    details: dict[str, Any] = Field(
        default_factory=dict, description="Structured context; never contains secrets"
    )


class ErrorEnvelope(BaseModel):
    """Standard error body (PART 38)."""

    error: ErrorDetail
    request_id: str | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": {
                    "code": "INSUFFICIENT_BALANCE",
                    "message": "Insufficient currency balance.",
                    "details": {"currency_code": "AFN"},
                },
                "request_id": "0f8b0f2e-6a11-4a5e-9b7c-1f5a2c8e9d10",
            }
        }
    )


class PageMeta(BaseModel):
    """Pagination metadata for list endpoints."""

    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)


class HealthResponse(BaseModel):
    """Liveness response — the process is running and serving requests."""

    status: str = Field(examples=["ok"])
    app: str
    version: str
    environment: str


class ComponentHealth(BaseModel):
    """State of one dependency."""

    name: str
    status: str = Field(description="ok | unavailable | degraded")
    detail: str | None = None
    latency_ms: float | None = None


class ReadinessResponse(BaseModel):
    """Readiness response — every required dependency answered."""

    status: str = Field(examples=["ready", "degraded"])
    environment: str
    components: list[ComponentHealth]
    schema_revision: str | None = Field(
        default=None, description="Applied Alembic revision; null when unmigrated"
    )


class VersionResponse(BaseModel):
    """Build metadata for operators and support."""

    app: str
    version: str
    environment: str
    git_sha: str
    build_time: str
    python_version: str
    schema_revision: str | None = None
    api_base_url: str = "/api/v1"
