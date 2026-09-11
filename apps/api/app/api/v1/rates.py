"""Exchange-rate endpoints (API_CONTRACT §9.2, PART 13).

Quotes are append-only: ``POST /rates`` adds a row and there is no ``PATCH``/``DELETE``.
Resolution (``GET /rates/resolve``) answers the question a counter actually asks — "what
rate applies to this pair, at this branch, now?" — using the Phase 0 database function,
so the endpoint and the future transaction cannot disagree.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.deps import (
    ClientIpDep,
    PrincipalDep,
    RateLimiterDep,
    RequestIdDep,
    actor_context,
    require_permission,
)
from app.core.exceptions import ValidationError
from app.core.permissions import Permission
from app.schemas.rates import (
    RateListResponse,
    RateQuote,
    RateQuoteRequest,
    RateResolution,
)
from app.services.audit_service import ActorContext
from app.services.ledger_master_service import RateService, build_rate_service

router = APIRouter(prefix="/rates", tags=["rates"])

_require_view = Depends(require_permission(Permission.EXCHANGE_VIEW))
_require_manage = Depends(require_permission(Permission.RATES_MANAGE))

_WINDOW_SECONDS = 60


def _service(request: Request) -> RateService:
    return build_rate_service(
        database=request.app.state.database, settings=request.app.state.settings
    )


def _actor(principal: PrincipalDep, *, ip: str | None, request_id: str | None) -> ActorContext:
    return actor_context(principal, ip_address=ip, request_id=request_id)


def _quote_from_mapping(row: Mapping[Any, Any]) -> RateQuote:
    """Serialize one quote row.

    Every write and read path builds the same mapping shape (the columns plus the
    currency and branch codes), so there is exactly one serializer and no field of the
    response model can silently stay empty.
    """
    return RateQuote(
        id=row["id"],
        from_currency_id=row["from_currency_id"],
        to_currency_id=row["to_currency_id"],
        from_currency_code=row["from_currency_code"],
        to_currency_code=row["to_currency_code"],
        buy_rate=row["buy_rate"],
        sell_rate=row["sell_rate"],
        effective_at=row["effective_at"],
        branch_id=row["branch_id"],
        branch_code=row["branch_code"],
        source=row["source"],
        created_at=row["created_at"],
        created_by=row["created_by"],
    )


@router.get(
    "",
    response_model=RateListResponse,
    summary="Current quotes (one per pair and branch)",
    dependencies=[_require_view],
)
async def current_rates(
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    branch_id: uuid.UUID | None = Query(
        default=None,
        description="Resolve for this branch (its own quote wins); omit for the global quote",
    ),
    from_currency_id: uuid.UUID | None = Query(default=None, description="Filter by pair"),
    to_currency_id: uuid.UUID | None = Query(default=None, description="Filter by pair"),
    at: dt.datetime | None = Query(default=None, description="Defaults to now (UTC)"),
) -> RateListResponse:
    """The quote in force for each pair — branch quotes override the global ones."""
    if from_currency_id is not None and to_currency_id is None:
        raise ValidationError(
            "A pair filter needs both currencies.",
            details={"fields": [{"field": "to_currency_id", "code": "missing"}]},
        )
    await limiter.enforce(
        f"read:rates:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_read_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    rows = await _service(request).current_quotes(
        branch_id=branch_id,
        at=at,
        from_currency_id=from_currency_id,
        to_currency_id=to_currency_id,
    )
    return RateListResponse(
        items=[_quote_from_mapping(row) for row in rows],
        total=len(rows),
        limit=len(rows),
        offset=0,
    )


@router.post(
    "",
    response_model=RateQuote,
    status_code=status.HTTP_201_CREATED,
    summary="Publish a quote (append-only)",
    dependencies=[_require_manage],
    responses={
        404: {"description": "Unknown currency or branch"},
        409: {"description": "Duplicate instant or a non-tradable currency"},
        422: {"description": "Schema violation or inactive currency/branch"},
    },
)
async def publish_rate(
    payload: RateQuoteRequest,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> RateQuote:
    """Append a quote and audit it as ``RATE_CREATED``."""
    await limiter.enforce(
        f"write:rates:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_write_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    rate = await _service(request).publish_rate(
        from_currency_id=payload.from_currency_id,
        to_currency_id=payload.to_currency_id,
        buy_rate=payload.buy_rate,
        sell_rate=payload.sell_rate,
        effective_at=payload.effective_at,
        branch_id=payload.branch_id,
        source=payload.source,
        actor=_actor(principal, ip=client_ip, request_id=request_id),
    )
    return _quote_from_mapping(rate)


@router.get(
    "/resolve",
    response_model=RateResolution,
    summary="Resolve the rate in force for a pair and branch",
    dependencies=[_require_view],
    responses={422: {"description": "No quote is in force (RATE_NOT_FOUND)"}},
)
async def resolve_rate(
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    from_currency_id: uuid.UUID = Query(...),
    to_currency_id: uuid.UUID = Query(...),
    branch_id: uuid.UUID | None = Query(default=None),
    at: dt.datetime | None = Query(default=None, description="Defaults to now (UTC)"),
) -> RateResolution:
    """The quote a transaction would receive right now (branch wins over global)."""
    await limiter.enforce(
        f"read:rates:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_read_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    row = await _service(request).resolve_rate(
        from_currency_id=from_currency_id,
        to_currency_id=to_currency_id,
        branch_id=branch_id,
        at=at,
    )
    return RateResolution(
        from_currency_id=from_currency_id,
        to_currency_id=to_currency_id,
        exchange_rate_id=row["exchange_rate_id"],
        buy_rate=row["buy_rate"],
        sell_rate=row["sell_rate"],
        effective_at=row["effective_at"],
        branch_id=row["branch_id"],
        is_branch_quote=row["branch_id"] is not None,
    )


@router.get(
    "/history",
    response_model=RateListResponse,
    summary="Quote history (newest first)",
    dependencies=[_require_view],
)
async def rate_history(
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    from_currency_id: uuid.UUID | None = Query(default=None),
    to_currency_id: uuid.UUID | None = Query(default=None),
    branch_id: uuid.UUID | None = Query(default=None),
    include_global: bool = Query(default=True),
    effective_from: dt.datetime | None = Query(default=None),
    effective_to: dt.datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> RateListResponse:
    """Every published quote in range — the audit view of who published what, when."""
    await limiter.enforce(
        f"read:rates:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_read_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    rows, total = await _service(request).list_history(
        from_currency_id=from_currency_id,
        to_currency_id=to_currency_id,
        branch_id=branch_id,
        include_global=include_global,
        effective_from=effective_from,
        effective_to=effective_to,
        limit=limit,
        offset=offset,
    )
    return RateListResponse(
        items=[_quote_from_mapping(row) for row in rows], total=total, limit=limit, offset=offset
    )
