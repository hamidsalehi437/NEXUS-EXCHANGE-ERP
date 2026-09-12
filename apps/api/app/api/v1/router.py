"""Aggregate router for ``/api/v1``.

Phase 1 mounted the system endpoints, Phase 2 authentication/administration, Phase 3 the
core master data (currencies, branches, customers, chart of accounts, exchange rates),
Phase 4 the ledger's read surface, Phase 5 the exchange documents themselves and Phase 6
cash control (shifts, movements, reconciliation). Later
feature routers (cash, transfers, sync, …) are added in the phase that implements them, so
the OpenAPI document never advertises an endpoint that does not work.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    accounts,
    auth,
    branches,
    cash,
    currencies,
    customers,
    devices,
    exchange,
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
api_router.include_router(exchange.router)
api_router.include_router(cash.router)
api_router.include_router(journal.router)
api_router.include_router(reports.router)

__all__ = ["api_router"]
