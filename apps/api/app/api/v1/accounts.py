"""Chart-of-accounts endpoints (API_CONTRACT §9.2, PART 11).

``accounts.manage`` guards both reads and writes: the chart is the accountant's working
surface, not counter data. ``normal_balance`` is derived from ``account_type`` on the
server and is shown in responses but never accepted from a client — a client that could
choose it could invert every balance report.
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
    ACCOUNT_TYPES,
    AccountCreateRequest,
    AccountListResponse,
    AccountResponse,
    AccountUpdateRequest,
)
from app.services.audit_service import ActorContext
from app.services.ledger_master_service import (
    AccountService,
    AccountView,
    build_account_service,
)

router = APIRouter(prefix="/accounts", tags=["accounts"])

_require_manage = Depends(require_permission(Permission.ACCOUNTS_MANAGE))

_WINDOW_SECONDS = 60


def _service(request: Request) -> AccountService:
    return build_account_service(
        database=request.app.state.database, settings=request.app.state.settings
    )


def _actor(principal: PrincipalDep, *, ip: str | None, request_id: str | None) -> ActorContext:
    return actor_context(principal, ip_address=ip, request_id=request_id)


def _response(view: AccountView) -> AccountResponse:
    """Render one account view; the model never leaks a padded CHAR or a missing flag."""
    account = view.account
    return AccountResponse(
        id=account.id,
        code=account.code,
        name=account.name,
        account_type=account.account_type,
        normal_balance=account.normal_balance,
        currency_id=account.currency_id,
        currency_code=view.currency_code,
        branch_id=account.branch_id,
        parent_id=account.parent_id,
        is_active=account.is_active,
        is_postable=account.is_postable,
        has_children=view.has_children,
        created_at=account.created_at,
        created_by=account.created_by,
    )


@router.get(
    "",
    response_model=AccountListResponse,
    summary="List the chart of accounts",
    dependencies=[_require_manage],
)
async def list_accounts(
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    account_type: str | None = Query(
        default=None, description=f"One of {', '.join(ACCOUNT_TYPES)}"
    ),
    branch_id: uuid.UUID | None = Query(default=None),
    currency_id: uuid.UUID | None = Query(default=None),
    parent_id: uuid.UUID | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    is_postable: bool | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> AccountListResponse:
    """Accounts ordered by code; ``parent_id`` reconstructs the tree."""
    await limiter.enforce(
        f"read:accounts:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_read_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    rows, total = await _service(request).list_accounts(
        account_type=account_type.upper() if account_type else None,
        branch_id=branch_id,
        currency_id=currency_id,
        parent_id=parent_id,
        is_active=is_active,
        is_postable=is_postable,
        limit=limit,
        offset=offset,
    )
    return AccountListResponse(
        items=[_response(view) for view in rows], total=total, limit=limit, offset=offset
    )


@router.post(
    "",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a ledger account",
    dependencies=[_require_manage],
    responses={
        404: {"description": "Unknown currency, branch or parent account"},
        409: {"description": "Duplicate code"},
        422: {"description": "Schema violation or inactive currency/branch"},
    },
)
async def create_account(
    payload: AccountCreateRequest,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> AccountResponse:
    """Create an account; the normal balance follows the account type."""
    await limiter.enforce(
        f"write:accounts:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_write_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    view = await _service(request).create_account(
        code=payload.code,
        name=payload.name,
        account_type=payload.account_type,
        currency_id=payload.currency_id,
        branch_id=payload.branch_id,
        parent_id=payload.parent_id,
        is_active=payload.is_active,
        is_postable=payload.is_postable,
        actor=_actor(principal, ip=client_ip, request_id=request_id),
    )
    return _response(view)


@router.get(
    "/{account_id}",
    response_model=AccountResponse,
    summary="Read one account",
    dependencies=[_require_manage],
)
async def get_account(
    account_id: uuid.UUID,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
) -> AccountResponse:
    """One account, with the flags a chart editor needs."""
    await limiter.enforce(
        f"read:accounts:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_read_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    return _response(await _service(request).get_account(account_id))


@router.patch(
    "/{account_id}",
    response_model=AccountResponse,
    summary="Update an account (identity frozen once it has journal lines)",
    dependencies=[_require_manage],
    responses={
        404: {"description": "Unknown account or parent"},
        409: {"description": "Code is frozen, duplicate, or the account has children"},
        422: {"description": "Schema violation, cycle or type mismatch"},
    },
)
async def update_account(
    account_id: uuid.UUID,
    payload: AccountUpdateRequest,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> AccountResponse:
    """Rename, re-parent, re-type (while unused) or deactivate an account."""
    await limiter.enforce(
        f"write:accounts:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_write_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    view = await _service(request).update_account(
        account_id=account_id,
        changes=payload.model_dump(exclude_unset=True),
        actor=_actor(principal, ip=client_ip, request_id=request_id),
    )
    return _response(view)
