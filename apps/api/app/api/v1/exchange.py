"""Exchange endpoints (API_CONTRACT §9.3, PART 63).

Six routes, and every financial decision behind them is taken by
:class:`app.services.exchange_service.ExchangeService`: the route validates the HTTP shape,
enforces the caller's permission (deny by default), rate-limits the call and renders the
document the service returns. There is no arithmetic, no scope rule and no ordering here —
that is what keeps a later phase from posting money from a request handler.

Two contract rules are visible at this layer:

* ``POST /exchange`` and both lifecycle moves require an ``Idempotency-Key`` header
  (PART 40). A retry with the same key replays the recorded answer instead of dealing again,
  and the key is scoped to ``(user, endpoint, key)`` so a key reused across endpoints can
  never be answered with the wrong operation's result.
* reads are scoped to the caller's branches, and a document outside that scope answers
  ``404`` rather than ``403``: the existence of another branch's trade is itself information.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.deps import (
    ClientIpDep,
    IdempotencyKeyDep,
    PrincipalDep,
    RateLimiterDep,
    RequestIdDep,
    actor_context,
    require_permission,
)
from app.core.permissions import Permission
from app.schemas.exchange import (
    ExchangeCreateRequest,
    ExchangeDocument,
    ExchangeListResponse,
    ExchangeReasonRequest,
    ExchangeReceipt,
)
from app.services.exchange_service import (
    ExchangeRequest,
    ExchangeResult,
    ExchangeService,
    ExchangeView,
    build_exchange_service,
)

router = APIRouter(prefix="/exchange", tags=["exchange"])

_require_create = Depends(require_permission(Permission.EXCHANGE_CREATE))
_require_view = Depends(require_permission(Permission.EXCHANGE_VIEW))
_require_cancel = Depends(require_permission(Permission.EXCHANGE_CANCEL))
_require_reverse = Depends(require_permission(Permission.EXCHANGE_REVERSE))

_WINDOW_SECONDS = 60

# The receipt is rendered by the client from the JSON payload. ``format=pdf`` arrives in
# Phase 11 with the reporting engine, and until then the parameter accepts only the format
# this phase can actually serve — an honest 422 instead of a stub that returns nothing.
ReceiptFormat = Literal["json"]


def _service(request: Request) -> ExchangeService:
    return build_exchange_service(
        database=request.app.state.database, settings=request.app.state.settings
    )


def _document(view: ExchangeView) -> ExchangeDocument:
    return ExchangeDocument.model_validate(view.to_payload())


def _created(result: ExchangeResult, response: Response) -> ExchangeDocument:
    """Render a create/cancel/reverse answer, replay included.

    A replay carries the status the original call answered with (201 for a create, 200 for a
    lifecycle move), which is what the client recorded against the key: answering a retry
    differently would make the key useless as a retry mechanism.
    """
    response.status_code = result.status_code
    return ExchangeDocument.model_validate(result.payload)


async def _enforce(
    request: Request, principal: PrincipalDep, limiter: RateLimiterDep, bucket: str
) -> None:
    settings = request.app.state.settings
    limit = (
        settings.rate_limit_write_per_min if bucket == "write" else settings.rate_limit_read_per_min
    )
    await limiter.enforce(
        f"{bucket}:exchange:{principal.user_id}", limit=limit, window_seconds=_WINDOW_SECONDS
    )


@router.post(
    "",
    response_model=ExchangeDocument,
    status_code=status.HTTP_201_CREATED,
    summary="Record an exchange (BUY or SELL)",
    dependencies=[_require_create],
    responses={
        400: {"description": "Missing or malformed Idempotency-Key"},
        403: {"description": "Missing permission, or a branch outside the caller's scope"},
        404: {"description": "Unknown branch, currency, customer or device"},
        409: {"description": "Rate out of tolerance, insufficient position, or a key conflict"},
        422: {"description": "Invalid deal, inactive currency/customer, or amount mismatch"},
    },
)
async def create_exchange(
    payload: ExchangeCreateRequest,
    request: Request,
    response: Response,
    idempotency_key: IdempotencyKeyDep,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> ExchangeDocument:
    """Post a deal: its document, journal entry, cash movements and audit rows, atomically.

    ``to_amount`` is computed by the server; a client that sends its own expectation is
    cross-checked, never trusted (PART 63).
    """
    await _enforce(request, principal, limiter, "write")
    result = await _service(request).create_exchange(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        request=ExchangeRequest(
            transaction_type=payload.transaction_type,
            branch_id=payload.branch_id,
            device_id=payload.device_id,
            customer_id=payload.customer_id,
            from_currency_id=payload.from_currency_id,
            from_amount=payload.from_amount,
            to_currency_id=payload.to_currency_id,
            exchange_rate=payload.exchange_rate,
            commission=payload.commission,
            to_amount=payload.to_amount,
            client_event_id=payload.client_event_id,
            transaction_date=payload.transaction_date,
            description=payload.description,
        ),
        idempotency_key=idempotency_key,
    )
    return _created(result, response)


@router.get(
    "",
    response_model=ExchangeListResponse,
    summary="List exchange transactions",
    dependencies=[_require_view],
)
async def list_exchanges(
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
    branch_id: uuid.UUID | None = Query(
        default=None, description="Defaults to every branch the caller may see"
    ),
    cashier_id: uuid.UUID | None = Query(default=None, description="Who recorded the deal"),
    customer_id: uuid.UUID | None = Query(default=None, description="Counterparty"),
    transaction_type: str | None = Query(default=None, description="BUY or SELL"),
    status_: str | None = Query(
        default=None, alias="status", description="PENDING, COMPLETED, CANCELLED or REVERSED"
    ),
    q: str | None = Query(default=None, description="Document number fragment"),
    from_: dt.datetime | None = Query(
        default=None, alias="from", description="Recorded at or after this instant (UTC)"
    ),
    to: dt.datetime | None = Query(default=None, description="Recorded before this instant (UTC)"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ExchangeListResponse:
    """The branch's exchange book, newest first, each row with its movements."""
    await _enforce(request, principal, limiter, "read")
    views, total = await _service(request).list_exchanges(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        branch_id=branch_id,
        cashier_id=cashier_id,
        customer_id=customer_id,
        transaction_type=transaction_type,
        status=status_,
        number_query=q,
        from_=from_,
        to=to,
        limit=limit,
        offset=offset,
    )
    return ExchangeListResponse(
        items=[_document(view) for view in views], total=total, limit=limit, offset=offset
    )


@router.get(
    "/{transaction_id}",
    response_model=ExchangeDocument,
    summary="Read one exchange transaction",
    dependencies=[_require_view],
    responses={404: {"description": "Unknown document, or outside the caller's branch scope"}},
)
async def get_exchange(
    transaction_id: uuid.UUID,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> ExchangeDocument:
    """The document, its applied rate, its journal reference and its reversal linkage."""
    await _enforce(request, principal, limiter, "read")
    view = await _service(request).get_exchange(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        transaction_id=transaction_id,
    )
    return _document(view)


@router.post(
    "/{transaction_id}/cancel",
    response_model=ExchangeDocument,
    status_code=status.HTTP_200_OK,
    summary="Cancel a completed exchange",
    dependencies=[_require_cancel],
    responses={
        400: {"description": "Missing or malformed Idempotency-Key"},
        404: {"description": "Unknown document, or outside the caller's branch scope"},
        409: {"description": "Not COMPLETED, already undone, or insufficient position"},
    },
)
async def cancel_exchange(
    transaction_id: uuid.UUID,
    payload: ExchangeReasonRequest,
    request: Request,
    response: Response,
    idempotency_key: IdempotencyKeyDep,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> ExchangeDocument:
    """Cancel a deal: the ledger posts a mirror entry, the document moves to ``CANCELLED``."""
    await _enforce(request, principal, limiter, "write")
    result = await _service(request).cancel_exchange(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        transaction_id=transaction_id,
        reason=payload.reason,
        idempotency_key=idempotency_key,
    )
    return _created(result, response)


@router.post(
    "/{transaction_id}/reverse",
    response_model=ExchangeDocument,
    status_code=status.HTTP_200_OK,
    summary="Reverse a completed exchange",
    dependencies=[_require_reverse],
    responses={
        400: {"description": "Missing or malformed Idempotency-Key"},
        404: {"description": "Unknown document, or outside the caller's branch scope"},
        409: {"description": "Not COMPLETED, already reversed, or insufficient position"},
    },
)
async def reverse_exchange(
    transaction_id: uuid.UUID,
    payload: ExchangeReasonRequest,
    request: Request,
    response: Response,
    idempotency_key: IdempotencyKeyDep,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> ExchangeDocument:
    """Reverse a deal: a mirror document, its journal entry and its cash movements."""
    await _enforce(request, principal, limiter, "write")
    result = await _service(request).reverse_exchange(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        transaction_id=transaction_id,
        reason=payload.reason,
        idempotency_key=idempotency_key,
    )
    return _created(result, response)


@router.get(
    "/{transaction_id}/receipt",
    response_model=ExchangeReceipt,
    summary="Deterministic receipt payload for one exchange",
    dependencies=[_require_view],
    responses={404: {"description": "Unknown document, or outside the caller's branch scope"}},
)
async def get_exchange_receipt(
    transaction_id: uuid.UUID,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
    receipt_format: Annotated[ReceiptFormat, Query(alias="format")] = "json",
) -> ExchangeReceipt:
    """Everything a printed or thermal receipt shows, read from committed rows.

    The payload is deterministic: asking twice for the same document returns the same bytes,
    including after later quotes are published — the receipt shows the document's applied rate.
    """
    await _enforce(request, principal, limiter, "read")
    del receipt_format  # the only supported format in Phase 5 is the JSON payload itself
    payload = await _service(request).build_receipt(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        transaction_id=transaction_id,
    )
    return ExchangeReceipt.model_validate(payload)
