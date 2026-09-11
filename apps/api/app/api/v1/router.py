"""Aggregate router for ``/api/v1``.

Phase 1 mounted the system endpoints, Phase 2 authentication/administration, and Phase 3
the core master data (currencies, branches, customers, chart of accounts, exchange
rates). Later feature routers (exchange, cash, transfers, reports, sync, …) are added in
the phase that implements them, so the OpenAPI document never advertises an endpoint that
does not work.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    accounts,
    auth,
    branches,
    currencies,
    customers,
    devices,
    health,
    journal,
    rates,
    reports,
    roles,
    users,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(roles.router)
api_router.include_router(devices.router)
api_router.include_router(currencies.router)
api_router.include_router(branches.router)
api_router.include_router(customers.router)
api_router.include_router(accounts.router)
api_router.include_router(rates.router)
api_router.include_router(journal.router)
api_router.include_router(reports.router)

__all__ = ["api_router"]
