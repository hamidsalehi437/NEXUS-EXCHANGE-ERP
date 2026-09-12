"""Cash endpoints (API_CONTRACT §9.4, PART 63).

Twelve routes over the shift lifecycle and the movement book. Every financial decision behind
them belongs to :class:`app.services.cash_service.CashService`; a route validates the HTTP
shape, enforces the caller's permission (deny by default), rate-limits the call and renders
what the service returned. There is no arithmetic, no scope rule and no ordering here — that
is what keeps a later phase from moving money from a request handler.

Three contract rules are visible at this layer:

* ``POST /cash/in``, ``POST /cash/out`` and ``POST /cash/close`` require an ``Idempotency-Key``
  (PART 40, API_CONTRACT §8). A retry with the same key replays the recorded answer instead of
  moving the money twice, and the key is scoped to ``(user, endpoint, key)``, so a key reused
  across endpoints can never be answered with another operation's result.
* ``POST /cash/open``, ``POST /cash/adjustment`` and the reversal accept a key as well; they
  honour it when it is sent (the same store), because a device coming back from an offline
  period should always be able to re-send a recorded act safely.
* reads are scoped to the caller's branches, and a shift or movement outside that scope answers
  ``404`` rather than ``403``: the existence of another branch's till is itself information.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.deps import (
    ClientIpDep,
    IdempotencyKeyDep,
    OptionalIdempotencyKeyDep,
    PrincipalDep,
    RateLimiterDep,
    RequestIdDep,
    actor_context,
    require_permission,
)
from app.core.exceptions import ResourceNotFoundError
from app.core.permissions import Permission
from app.schemas.cash import (
    CashAdjustmentRequest,
    CashBalanceResponse,
    CashBalanceRowResponse,
    CashCloseRequest,
    CashInRequest,
    CashMovementListResponse,
    CashMovementResponse,
    CashOpenRequest,
    CashOutRequest,
    CashReverseRequest,
    CashSessionListResponse,
    CashSessionResponse,
)
from app.services.cash_service import (
    CashCount,
    CashMovementRequest,
    CashOpening,
    CashService,
    CloseSessionRequest,
    OpenSessionRequest,
    build_cash_service,
)

router = APIRouter(prefix="/cash", tags=["cash"])

_require_create = Depends(require_permission(Permission.CASH_CREATE))
_require_view = Depends(require_permission(Permission.CASH_VIEW))
_require_close = Depends(require_permission(Permission.CASH_CLOSE))
_require_adjust = Depends(require_permission(Permission.CASH_ADJUST))

_WINDOW_SECONDS = 60


def _service(request: Request) -> CashService:
    return build_cash_service(
        database=request.app.state.database, settings=request.app.state.settings
    )


def _movement(payload: Mapping[str, Any]) -> CashMovementResponse:
    return CashMovementResponse.model_validate(payload)


def _session(payload: Mapping[str, Any]) -> CashSessionResponse:
    return CashSessionResponse.model_validate(payload)


async def _enforce(
    request: Request, principal: PrincipalDep, limiter: RateLimiterDep, bucket: str
) -> None:
    settings = request.app.state.settings
    limit = (
        settings.rate_limit_write_per_min if bucket == "write" else settings.rate_limit_read_per_min
    )
    await limiter.enforce(
        f"{bucket}:cash:{principal.user_id}", limit=limit, window_seconds=_WINDOW_SECONDS
    )


@router.get(
    "/balance",
    response_model=CashBalanceResponse,
    summary="Cash position per branch and currency",
    dependencies=[_require_view],
    responses={403: {"description": "Missing permission, or a branch outside the caller's scope"}},
)
async def cash_balance(
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
    branch_id: uuid.UUID | None = Query(
        default=None, description="Defaults to every branch the caller may see"
    ),
) -> CashBalanceResponse:
    """The drawers' physical position beside what the journal carries, never aggregated.

    A currency is never summed with another: a position is a quantity of one currency at one
    branch, and the functional figure beside it is the ledger's valuation, not a conversion.
    """
    await _enforce(request, principal, limiter, "read")
    rows = await _service(request).balance(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        branch_id=branch_id,
    )
    return CashBalanceResponse(
        items=[CashBalanceRowResponse.model_validate(row.to_payload()) for row in rows],
        source="cash_movements",
        generated_at=dt.datetime.now(dt.UTC),
    )


@router.post(
    "/open",
    response_model=CashSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Open a cash session (shift)",
    dependencies=[_require_create],
    responses={
        403: {"description": "Missing permission, or a branch outside the caller's scope"},
        404: {"description": "Unknown branch, currency or device"},
        409: {
            "description": (
                "A shift is already open at this branch, or the declared opening disagrees "
                "with the cash the books carry"
            )
        },
        422: {"description": "Invalid opening balance or an inactive currency"},
    },
)
async def open_session(
    payload: CashOpenRequest,
    request: Request,
    response: Response,
    idempotency_key: OptionalIdempotencyKeyDep,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> CashSessionResponse:
    """Open a shift and record what the operator counted.

    A drawer the books do not know yet gets an opening entry (``Dr Cash / Cr 6000``, §6.1);
    a drawer the books already carry must be counted at exactly that amount, and a
    disagreement is refused rather than posted.
    """
    await _enforce(request, principal, limiter, "write")
    result = await _service(request).open_session(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        request=OpenSessionRequest(
            branch_id=payload.branch_id,
            device_id=payload.device_id,
            openings=[
                CashOpening(
                    currency_id=line.currency_id,
                    amount=line.amount,
                    exchange_rate=line.exchange_rate,
                )
                for line in payload.openings
            ],
            notes=payload.notes,
        ),
        idempotency_key=idempotency_key,
    )
    response.status_code = result.status_code
    return _session(dict(result.payload))


@router.post(
    "/in",
    response_model=CashMovementResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record cash received into the drawer",
    dependencies=[_require_create],
    responses={
        400: {"description": "Missing or malformed Idempotency-Key"},
        403: {"description": "Missing permission, or a branch outside the caller's scope"},
        404: {"description": "Unknown branch, currency, account or session"},
        409: {"description": "No open shift, or a key conflict"},
        422: {"description": "Missing source account, inactive currency, or a bad amount"},
    },
)
async def cash_in(
    payload: CashInRequest,
    request: Request,
    response: Response,
    idempotency_key: IdempotencyKeyDep,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> CashMovementResponse:
    """Post a receipt: journal entry, movement and audit row, in one transaction."""
    await _enforce(request, principal, limiter, "write")
    result = await _service(request).record_in(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        request=CashMovementRequest(
            branch_id=payload.branch_id,
            currency_id=payload.currency_id,
            amount=payload.amount,
            session_id=payload.session_id,
            device_id=payload.device_id,
            counter_account_id=payload.source_account_id,
            description=payload.description,
            client_event_id=payload.client_event_id,
            transaction_date=payload.transaction_date,
        ),
        idempotency_key=idempotency_key,
    )
    response.status_code = result.status_code
    return _movement(dict(result.payload))


@router.post(
    "/out",
    response_model=CashMovementResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record cash paid out of the drawer",
    dependencies=[_require_create],
    responses={
        400: {"description": "Missing or malformed Idempotency-Key"},
        403: {"description": "Missing permission, or a branch outside the caller's scope"},
        404: {"description": "Unknown branch, currency, account or session"},
        409: {
            "description": (
                "No open shift, insufficient position in that drawer (with the shortfall), "
                "or a key conflict"
            )
        },
        422: {"description": "Missing target account, inactive currency, or a bad amount"},
    },
)
async def cash_out(
    payload: CashOutRequest,
    request: Request,
    response: Response,
    idempotency_key: IdempotencyKeyDep,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> CashMovementResponse:
    """Post a payout: the drawer's position is checked under its lock, then the entry is cut."""
    await _enforce(request, principal, limiter, "write")
    result = await _service(request).record_out(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        request=CashMovementRequest(
            branch_id=payload.branch_id,
            currency_id=payload.currency_id,
            amount=payload.amount,
            session_id=payload.session_id,
            device_id=payload.device_id,
            counter_account_id=payload.target_account_id,
            description=payload.description,
            client_event_id=payload.client_event_id,
            transaction_date=payload.transaction_date,
        ),
        idempotency_key=idempotency_key,
    )
    response.status_code = result.status_code
    return _movement(dict(result.payload))


@router.post(
    "/adjustment",
    response_model=CashMovementResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a cash short/over adjustment",
    dependencies=[_require_adjust],
    responses={
        403: {"description": "Missing cash.adjust, or a branch outside the caller's scope"},
        404: {"description": "Unknown branch, currency or session"},
        409: {
            "description": "No open shift, insufficient position to take the shortage out, "
            "or a key conflict"
        },
        422: {"description": "Missing direction or reason, inactive currency, or a bad amount"},
    },
)
async def cash_adjustment(
    payload: CashAdjustmentRequest,
    request: Request,
    response: Response,
    idempotency_key: OptionalIdempotencyKeyDep,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> CashMovementResponse:
    """Post a correction against ``5090 Cash Short / Over`` (§6.4), with its reason."""
    await _enforce(request, principal, limiter, "write")
    result = await _service(request).record_adjustment(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        request=CashMovementRequest(
            branch_id=payload.branch_id,
            currency_id=payload.currency_id,
            amount=payload.amount,
            session_id=payload.session_id,
            device_id=payload.device_id,
            adjustment_sign=payload.adjustment_sign,
            reason=payload.reason,
            description=payload.description,
            client_event_id=payload.client_event_id,
            transaction_date=payload.transaction_date,
        ),
        idempotency_key=idempotency_key,
    )
    response.status_code = result.status_code
    return _movement(dict(result.payload))


@router.post(
    "/close",
    response_model=CashSessionResponse,
    status_code=status.HTTP_200_OK,
    summary="Close a cash session against a physical count",
    dependencies=[_require_close],
    responses={
        400: {"description": "Missing or malformed Idempotency-Key"},
        403: {
            "description": (
                "Missing permission, another operator's shift, or a difference without cash.adjust"
            )
        },
        404: {"description": "Unknown session, or outside the caller's branch scope"},
        409: {"description": "The shift is already closed, or a key conflict"},
        422: {"description": "An incomplete count, or a currency the shift never moved"},
    },
)
async def close_session(
    payload: CashCloseRequest,
    request: Request,
    response: Response,
    idempotency_key: IdempotencyKeyDep,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> CashSessionResponse:
    """Reconcile the shift: expected is derived, the difference is posted, the session closes.

    Every currency the shift moved has to be counted (``CASH_RECON_INCOMPLETE`` otherwise); a
    non-zero difference is posted to ``5090`` under ``cash.adjust`` and reported in
    ``variances``, and the count itself is never overwritten.
    """
    await _enforce(request, principal, limiter, "write")
    result = await _service(request).close_session(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        request=CloseSessionRequest(
            session_id=payload.session_id,
            counted=[
                CashCount(currency_id=line.currency_id, amount=line.amount)
                for line in payload.counted
            ],
            notes=payload.notes,
        ),
        idempotency_key=idempotency_key,
    )
    response.status_code = result.status_code
    return _session(dict(result.payload))


@router.get(
    "/movements",
    response_model=CashMovementListResponse,
    summary="List cash movements",
    dependencies=[_require_view],
)
async def list_movements(
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
    branch_id: uuid.UUID | None = Query(
        default=None, description="Defaults to every branch the caller may see"
    ),
    currency_id: uuid.UUID | None = Query(default=None),
    movement_type: str | None = Query(
        default=None, description="OPENING, IN, OUT, ADJUSTMENT, EXPENSE or CLOSING"
    ),
    session_id: uuid.UUID | None = Query(default=None, description="One shift's movements"),
    from_: dt.datetime | None = Query(
        default=None, alias="from", description="Recorded at or after this instant (UTC)"
    ),
    to: dt.datetime | None = Query(default=None, description="Recorded before this instant (UTC)"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> CashMovementListResponse:
    """The branch's movement book, newest first, each row with its reversal linkage."""
    await _enforce(request, principal, limiter, "read")
    views, total = await _service(request).list_movements(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        branch_id=branch_id,
        currency_id=currency_id,
        movement_type=movement_type,
        session_id=session_id,
        from_=from_,
        to=to,
        limit=limit,
        offset=offset,
    )
    return CashMovementListResponse(
        items=[_movement(view.to_payload()) for view in views],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/movements/{movement_id}",
    response_model=CashMovementResponse,
    summary="Read one cash movement",
    dependencies=[_require_view],
    responses={404: {"description": "Unknown movement, or outside the caller's branch scope"}},
)
async def get_movement(
    movement_id: uuid.UUID,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> CashMovementResponse:
    """The movement, its journal reference and the movement that reversed it, if any."""
    await _enforce(request, principal, limiter, "read")
    view = await _service(request).get_movement(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        movement_id=movement_id,
    )
    return _movement(view.to_payload())


@router.post(
    "/movements/{movement_id}/reverse",
    response_model=CashMovementResponse,
    status_code=status.HTTP_200_OK,
    summary="Reverse one cash movement",
    dependencies=[_require_adjust],
    responses={
        403: {"description": "Missing cash.adjust, or a branch outside the caller's scope"},
        404: {"description": "Unknown movement, or outside the caller's branch scope"},
        409: {
            "description": (
                "Already reversed, belongs to another document (an exchange deal or a shift "
                "opening), or would take the drawer below zero (with the shortfall)"
            )
        },
        422: {"description": "Missing reason"},
    },
)
async def reverse_movement(
    movement_id: uuid.UUID,
    payload: CashReverseRequest,
    request: Request,
    response: Response,
    idempotency_key: OptionalIdempotencyKeyDep,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> CashMovementResponse:
    """Post the compensating entry and movement; the original stays exactly as posted."""
    await _enforce(request, principal, limiter, "write")
    result = await _service(request).reverse_movement(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        movement_id=movement_id,
        reason=payload.reason,
        idempotency_key=idempotency_key,
    )
    response.status_code = result.status_code
    return _movement(dict(result.payload))


@router.get(
    "/sessions",
    response_model=CashSessionListResponse,
    summary="List cash sessions (shift history)",
    dependencies=[_require_view],
)
async def list_sessions(
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
    branch_id: uuid.UUID | None = Query(
        default=None, description="Defaults to every branch the caller may see"
    ),
    status_: Annotated[str | None, Query(alias="status", description="OPEN or CLOSED")] = None,
    device_id: uuid.UUID | None = Query(default=None),
    opened_by: uuid.UUID | None = Query(default=None, description="The operator who opened it"),
    from_: dt.datetime | None = Query(default=None, alias="from"),
    to: dt.datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> CashSessionListResponse:
    """Shifts with their expected/counted/difference lines, newest first."""
    await _enforce(request, principal, limiter, "read")
    views, total = await _service(request).list_sessions(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        branch_id=branch_id,
        status=status_,
        device_id=device_id,
        opened_by=opened_by,
        from_=from_,
        to=to,
        limit=limit,
        offset=offset,
    )
    return CashSessionListResponse(
        items=[_session(view.to_payload()) for view in views],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/sessions/current",
    response_model=CashSessionResponse,
    summary="The caller's open cash session",
    dependencies=[_require_view],
    responses={
        403: {"description": "Missing permission, or a branch outside the caller's scope"},
        404: {"description": "No shift is open for that drawer"},
    },
)
async def current_session(
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
    branch_id: uuid.UUID | None = Query(
        default=None, description="Defaults to the caller's own branch"
    ),
    device_id: uuid.UUID | None = Query(
        default=None, description="Defaults to the caller's device"
    ),
) -> CashSessionResponse:
    """The shift a reconnected client has to reconcile with before it keeps recording."""
    await _enforce(request, principal, limiter, "read")
    view = await _service(request).current_session(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        branch_id=branch_id,
        device_id=device_id,
    )
    if view is None:
        raise _no_open_session(branch_id or principal.branch_id)
    return _session(view.to_payload())


@router.get(
    "/sessions/{session_id}",
    response_model=CashSessionResponse,
    summary="Read one cash session",
    dependencies=[_require_view],
    responses={404: {"description": "Unknown session, or outside the caller's branch scope"}},
)
async def get_session(
    session_id: uuid.UUID,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> CashSessionResponse:
    """The shift with its reconciliation lines and every movement it recorded."""
    await _enforce(request, principal, limiter, "read")
    view = await _service(request).get_session(
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
        session_id=session_id,
    )
    return _session(view.to_payload())


def _no_open_session(branch_id: uuid.UUID | None) -> ResourceNotFoundError:
    """The refusal a client uses to tell "no shift is open" from "something went wrong"."""
    return ResourceNotFoundError(
        "No cash session is open for that drawer.",
        details={
            "resource": "cash_session",
            "reason": "NO_OPEN_SESSION",
            "branch_id": str(branch_id) if branch_id else None,
        },
    )
