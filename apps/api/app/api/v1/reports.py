"""Report endpoints (API_CONTRACT §9).

Phase 4 ships the one report the accounting engine can already prove: the trial balance,
derived from ``journal_lines`` and carrying its own check (Σdebit = Σcredit, PART 49).
Reports that need the exchange, cash or transfer modules arrive with those phases — an
endpoint that answers with a half-built number is worse than a missing endpoint.
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
from app.schemas.journal import TrialBalanceResponse
from app.services.accounting_service import AccountingService, build_accounting_service

router = APIRouter(prefix="/reports", tags=["reports"])

_require_view = Depends(require_permission(Permission.REPORTS_VIEW))

_WINDOW_SECONDS = 60


def _service(request: Request) -> AccountingService:
    return build_accounting_service(
        database=request.app.state.database, settings=request.app.state.settings
    )


@router.get(
    "/trial-balance",
    response_model=TrialBalanceResponse,
    summary="Trial balance with the Σdebit = Σcredit proof",
    dependencies=[_require_view],
    responses={403: {"description": "A branch outside the caller's scope was requested"}},
)
async def trial_balance(
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
    branch_id: uuid.UUID | None = Query(
        default=None, description="Defaults to the caller's own branch"
    ),
    account_type: str | None = Query(
        default=None, description="ASSET, LIABILITY, EQUITY, REVENUE or EXPENSE"
    ),
    from_: dt.datetime | None = Query(
        default=None, alias="from", description="Entries dated at or after this instant (UTC)"
    ),
    to: dt.datetime | None = Query(
        default=None, description="Entries dated before this instant (UTC)"
    ),
    include_inactive: bool = Query(
        default=True, description="Keep accounts that have been deactivated"
    ),
) -> TrialBalanceResponse:
    """Per account and currency, read from the immutable lines rather than the cache."""
    await limiter.enforce(
        f"read:reports:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_read_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    view = await _service(request).get_trial_balance(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        branch_id=branch_id,
        account_type=account_type,
        from_=from_,
        to=to,
        include_inactive=include_inactive,
    )
    return TrialBalanceResponse.model_validate(view.to_payload())
