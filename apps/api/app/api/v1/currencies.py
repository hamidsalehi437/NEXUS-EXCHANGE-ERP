"""Currency endpoints (API_CONTRACT §9.2).

Reads need ``exchange.view``; writes need ``settings.manage`` (currencies are business
settings, and exactly one of them is the reporting currency of every report). The
``code`` is immutable: it is absent from the update schema, so the API cannot even
express the change the database would refuse.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.deps import (
    ClientIpDep,
    PrincipalDep,
    RateLimiterDep,
    RequestIdDep,
    actor_context,
    require_permission,
)
from app.core.permissions import Permission
from app.schemas.masterdata import (
    CurrencyCreateRequest,
    CurrencyListResponse,
    CurrencyResponse,
    CurrencyUpdateRequest,
)
from app.services.audit_service import ActorContext
from app.services.masterdata_service import MasterDataService, build_master_data_service

router = APIRouter(prefix="/currencies", tags=["currencies"])

_require_view = Depends(require_permission(Permission.EXCHANGE_VIEW))
_require_manage = Depends(require_permission(Permission.SETTINGS_MANAGE))

# Reads are bounded by the shared read bucket; a counter refreshing a currency picker
# must not be able to exhaust the write allowance (PART 42, API_CONTRACT §7).
_READ_WINDOW_SECONDS = 60


def _service(request: Request) -> MasterDataService:
    return build_master_data_service(
        database=request.app.state.database, settings=request.app.state.settings
    )


def _actor(principal: PrincipalDep, *, ip: str | None, request_id: str | None) -> ActorContext:
    return actor_context(principal, ip_address=ip, request_id=request_id)


def _response(currency: object) -> CurrencyResponse:
    return CurrencyResponse(
        id=currency.id,  # type: ignore[attr-defined]
        code=currency.code,  # type: ignore[attr-defined]
        name=currency.name,  # type: ignore[attr-defined]
        symbol=currency.symbol,  # type: ignore[attr-defined]
        decimal_places=currency.decimal_places,  # type: ignore[attr-defined]
        is_base=currency.is_base,  # type: ignore[attr-defined]
        is_active=currency.is_active,  # type: ignore[attr-defined]
        is_tradable=currency.is_tradable,  # type: ignore[attr-defined]
        display_order=currency.display_order,  # type: ignore[attr-defined]
        created_at=currency.created_at,  # type: ignore[attr-defined]
    )


@router.get(
    "",
    response_model=CurrencyListResponse,
    summary="List currencies",
    dependencies=[_require_view],
)
async def list_currencies(
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    is_active: bool | None = Query(default=None),
    is_tradable: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> CurrencyListResponse:
    """Every currency the business may quote or settle in."""
    await limiter.enforce(
        f"read:currencies:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_read_per_min,
        window_seconds=_READ_WINDOW_SECONDS,
    )
    rows, total = await _service(request).list_currencies(
        is_active=is_active, is_tradable=is_tradable, limit=limit, offset=offset
    )
    return CurrencyListResponse(
        items=[_response(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.post(
    "",
    response_model=CurrencyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a currency",
    dependencies=[_require_manage],
    responses={
        409: {"description": "Duplicate code or a conflicting base-currency change"},
        422: {"description": "Schema violation"},
    },
)
async def create_currency(
    payload: CurrencyCreateRequest,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> CurrencyResponse:
    """Create a currency (audit ``CURRENCY_CREATED``)."""
    await limiter.enforce(
        f"write:currencies:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_write_per_min,
        window_seconds=_READ_WINDOW_SECONDS,
    )
    currency = await _service(request).create_currency(
        code=payload.code,
        name=payload.name,
        symbol=payload.symbol,
        decimal_places=payload.decimal_places,
        is_base=payload.is_base,
        is_active=payload.is_active,
        is_tradable=payload.is_tradable,
        display_order=payload.display_order,
        actor=_actor(principal, ip=client_ip, request_id=request_id),
    )
    return _response(currency)


@router.get(
    "/{currency_id}",
    response_model=CurrencyResponse,
    summary="Read one currency",
    dependencies=[_require_view],
)
async def get_currency(
    currency_id: uuid.UUID,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
) -> CurrencyResponse:
    """One currency by id."""
    await limiter.enforce(
        f"read:currencies:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_read_per_min,
        window_seconds=_READ_WINDOW_SECONDS,
    )
    return _response(await _service(request).get_currency(currency_id))


@router.patch(
    "/{currency_id}",
    response_model=CurrencyResponse,
    summary="Update a currency (code is immutable)",
    dependencies=[_require_manage],
    responses={
        409: {"description": "Base-currency rule violated"},
        422: {"description": "Schema violation"},
    },
)
async def update_currency(
    currency_id: uuid.UUID,
    payload: CurrencyUpdateRequest,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> CurrencyResponse:
    """Update name, symbol, decimals, tradability or the active/base flags."""
    await limiter.enforce(
        f"write:currencies:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_write_per_min,
        window_seconds=_READ_WINDOW_SECONDS,
    )
    currency = await _service(request).update_currency(
        currency_id=currency_id,
        changes=payload.model_dump(exclude_unset=True),
        actor=_actor(principal, ip=client_ip, request_id=request_id),
    )
    return _response(currency)
