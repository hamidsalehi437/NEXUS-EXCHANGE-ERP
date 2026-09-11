"""Journal endpoints (API_CONTRACT §8, PART 49).

The ledger is readable and nothing else: ``GET /journal`` and ``GET /journal/{id}`` show
posted entries with their lines, totals and reversal linkage. There is no ``POST``,
``PATCH`` or ``DELETE`` here — an entry is posted by the service that owns the business
document that caused it (PART 46), and a mistake is corrected with a reversal, never by
editing history. A route that could mutate a journal would be a route that could break
PART 49, so none exists.

Both endpoints are guarded by ``reports.view`` (API_CONTRACT §8) and rate-limited per
user, and every read is scoped to the caller's branch unless they hold a global scope
role. An entry outside that scope answers ``404`` rather than ``403``: telling a cashier
that another branch's entry exists is itself a disclosure.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import (
    ClientIpDep,
    PrincipalDep,
    RateLimiterDep,
    RequestIdDep,
    actor_context,
    require_permission,
)
from app.core.permissions import Permission
from app.schemas.journal import JournalEntryListResponse, JournalEntryResponse
from app.services.accounting_service import AccountingService, build_accounting_service

router = APIRouter(prefix="/journal", tags=["journal"])

_require_view = Depends(require_permission(Permission.REPORTS_VIEW))

_WINDOW_SECONDS = 60


def _service(request: Request) -> AccountingService:
    return build_accounting_service(
        database=request.app.state.database, settings=request.app.state.settings
    )


@router.get(
    "",
    response_model=JournalEntryListResponse,
    summary="List posted journal entries",
    dependencies=[_require_view],
)
async def list_journal_entries(
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
    reference_type: str | None = Query(
        default=None, description="EXCHANGE_TRANSACTION, CASH_MOVEMENT, EXPENSE, REVERSAL, …"
    ),
    reference_id: uuid.UUID | None = Query(
        default=None, description="The business document the entry belongs to"
    ),
    branch_id: uuid.UUID | None = Query(
        default=None, description="Defaults to every branch the caller may see"
    ),
    from_: dt.datetime | None = Query(
        default=None, alias="from", description="Entries dated at or after this instant (UTC)"
    ),
    to: dt.datetime | None = Query(
        default=None, description="Entries dated before this instant (UTC)"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> JournalEntryListResponse:
    """Entries newest first, each with its totals and reversal linkage."""
    await limiter.enforce(
        f"read:journal:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_read_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    views, total = await _service(request).list_journal_entries(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        reference_type=reference_type,
        reference_id=reference_id,
        branch_id=branch_id,
        from_=from_,
        to=to,
        limit=limit,
        offset=offset,
    )
    return JournalEntryListResponse(
        items=[JournalEntryResponse.model_validate(view.to_payload()) for view in views],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{entry_id}",
    response_model=JournalEntryResponse,
    summary="One journal entry with its lines",
    dependencies=[_require_view],
    responses={404: {"description": "Unknown entry, or outside the caller's branch scope"}},
)
async def get_journal_entry(
    entry_id: uuid.UUID,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> JournalEntryResponse:
    """The entry, its lines, and the reversal links in both directions."""
    await limiter.enforce(
        f"read:journal:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_read_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    view = await _service(request).get_journal_entry(
        entry_id=entry_id,
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
    )
    return JournalEntryResponse.model_validate(view.to_payload())
