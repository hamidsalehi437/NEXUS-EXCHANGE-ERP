"""Customer endpoints (API_CONTRACT §9.2, PART 10, PART 65).

``customer.create``, ``customer.view`` and ``customer.update`` are three separate
permissions on purpose: a cashier registers the walk-in customer standing at the counter
and may read the profile, but editing an existing customer's details (or deactivating
them) is a manager/accountant action. ``DELETE`` is a documented soft delete — the row is
never removed, because it is referenced by transactions and transfers that must keep
resolving forever (PART 25).
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
from app.schemas.customers import (
    CustomerCreateRequest,
    CustomerListResponse,
    CustomerResponse,
    CustomerUpdateRequest,
)
from app.services.audit_service import ActorContext
from app.services.masterdata_service import MasterDataService, build_master_data_service

router = APIRouter(prefix="/customers", tags=["customers"])

_require_view = Depends(require_permission(Permission.CUSTOMER_VIEW))
_require_create = Depends(require_permission(Permission.CUSTOMER_CREATE))
_require_update = Depends(require_permission(Permission.CUSTOMER_UPDATE))

_WINDOW_SECONDS = 60


def _service(request: Request) -> MasterDataService:
    return build_master_data_service(
        database=request.app.state.database, settings=request.app.state.settings
    )


def _actor(principal: PrincipalDep, *, ip: str | None, request_id: str | None) -> ActorContext:
    return actor_context(principal, ip_address=ip, request_id=request_id)


def _response(customer: object) -> CustomerResponse:
    return CustomerResponse(
        id=customer.id,  # type: ignore[attr-defined]
        customer_code=customer.customer_code,  # type: ignore[attr-defined]
        full_name=customer.full_name,  # type: ignore[attr-defined]
        phone=customer.phone,  # type: ignore[attr-defined]
        address=customer.address,  # type: ignore[attr-defined]
        notes=customer.notes,  # type: ignore[attr-defined]
        national_id_last4=customer.national_id_last4,  # type: ignore[attr-defined]
        branch_id=customer.branch_id,  # type: ignore[attr-defined]
        is_active=customer.is_active,  # type: ignore[attr-defined]
        created_at=customer.created_at,  # type: ignore[attr-defined]
        updated_at=customer.updated_at,  # type: ignore[attr-defined]
        created_by=customer.created_by,  # type: ignore[attr-defined]
        updated_by=customer.updated_by,  # type: ignore[attr-defined]
    )


@router.get(
    "",
    response_model=CustomerListResponse,
    summary="Search customers",
    dependencies=[_require_view],
)
async def list_customers(
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    q: str | None = Query(default=None, max_length=100, description="Name, phone or code"),
    branch_id: uuid.UUID | None = Query(default=None),
    shared_only: bool = Query(
        default=False, description="Only customers shared across branches (branch_id IS NULL)"
    ),
    is_active: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> CustomerListResponse:
    """Search by name, phone or code; newest first."""
    await limiter.enforce(
        f"read:customers:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_read_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    rows, total = await _service(request).list_customers(
        branch_id=branch_id,
        shared_only=shared_only,
        is_active=is_active,
        search=q,
        limit=limit,
        offset=offset,
    )
    return CustomerListResponse(
        items=[_response(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.post(
    "",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a customer",
    dependencies=[_require_create],
    responses={
        404: {"description": "Unknown branch"},
        409: {"description": "Duplicate customer code"},
        422: {"description": "Schema violation or inactive branch"},
    },
)
async def create_customer(
    payload: CustomerCreateRequest,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> CustomerResponse:
    """Register a customer; the code is issued when the caller does not supply one."""
    await limiter.enforce(
        f"write:customers:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_write_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    customer = await _service(request).create_customer(
        full_name=payload.full_name,
        phone=payload.phone,
        address=payload.address,
        notes=payload.notes,
        national_id_last4=payload.national_id_last4,
        branch_id=payload.branch_id,
        customer_code=payload.customer_code,
        is_active=payload.is_active,
        actor=_actor(principal, ip=client_ip, request_id=request_id),
    )
    return _response(customer)


@router.get(
    "/{customer_id}",
    response_model=CustomerResponse,
    summary="Read one customer",
    dependencies=[_require_view],
)
async def get_customer(
    customer_id: uuid.UUID,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
) -> CustomerResponse:
    """One customer profile."""
    await limiter.enforce(
        f"read:customers:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_read_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    return _response(await _service(request).get_customer(customer_id))


@router.patch(
    "/{customer_id}",
    response_model=CustomerResponse,
    summary="Update a customer (audited field diff)",
    dependencies=[_require_update],
    responses={
        404: {"description": "Unknown customer or branch"},
        422: {"description": "Schema violation"},
    },
)
async def update_customer(
    customer_id: uuid.UUID,
    payload: CustomerUpdateRequest,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> CustomerResponse:
    """Update a customer; the audit row records the old and new value of every change."""
    await limiter.enforce(
        f"write:customers:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_write_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    customer = await _service(request).update_customer(
        customer_id=customer_id,
        changes=payload.model_dump(exclude_unset=True),
        actor=_actor(principal, ip=client_ip, request_id=request_id),
    )
    return _response(customer)


@router.delete(
    "/{customer_id}",
    response_model=CustomerResponse,
    summary="Deactivate a customer (soft delete)",
    dependencies=[_require_update],
    responses={404: {"description": "Unknown customer"}},
)
async def deactivate_customer(
    customer_id: uuid.UUID,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> CustomerResponse:
    """Deactivate a customer — the row stays for every transaction that references it."""
    await limiter.enforce(
        f"write:customers:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_write_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    customer = await _service(request).deactivate_customer(
        customer_id=customer_id,
        actor=_actor(principal, ip=client_ip, request_id=request_id),
    )
    return _response(customer)
