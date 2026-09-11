"""Aggregate router for ``/api/v1``.

Phase 1 mounted the system endpoints. Phase 2 adds authentication, user
administration, the role/permission catalogue and device management. Later feature
routers (currencies, exchange, cash, reports, sync, …) are added in the phase that
implements them, so the OpenAPI document never advertises an endpoint that does not
work.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth, devices, health, roles, users

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(roles.router)
api_router.include_router(devices.router)

__all__ = ["api_router"]
