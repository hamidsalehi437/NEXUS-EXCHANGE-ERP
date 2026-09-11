"""Aggregate router for ``/api/v1``.

Phase 1 mounts the system endpoints only. Feature routers (auth, currencies,
exchange, cash, reports, sync, …) are added in the phase that implements them, so
the OpenAPI document never advertises an endpoint that does not work.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import health

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)

__all__ = ["api_router"]
