"""Branch endpoints (API_CONTRACT §9.1).

``branch.manage`` covers both reads and writes: the branch list is administrative
information (addresses, timezones, which office exists), not operational data a cashier
needs, so it stays behind the same permission that guards the write.
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
    BranchCreateRequest,
    BranchListResponse,
    BranchResponse,
    BranchUpdateRequest,
)
from app.services.audit_service import ActorContext
from app.services.masterdata_service import MasterDataService, build_master_data_service

router = APIRouter(prefix="/branches", tags=["branches"])

_require_manage = Depends(require_permission(Permission.BRANCH_MANAGE))

_WINDOW_SECONDS = 60


def _service(request: Request) -> MasterDataService:
    return build_master_data_service(
        database=request.app.state.database, settings=request.app.state.settings
    )


def _actor(principal: PrincipalDep, *, ip: str | None, request_id: str | None) -> ActorContext:
    return actor_context(principal, ip_address=ip, request_id=request_id)


def _response(branch: object) -> BranchResponse:
    return BranchResponse(
        id=branch.id,  # type: ignore[attr-defined]
        code=branch.code,  # type: ignore[attr-defined]
        name=branch.name,  # type: ignore[attr-defined]
        address=branch.address,  # type: ignore[attr-defined]
        phone=branch.phone,  # type: ignore[attr-defined]
        is_active=branch.is_active,  # type: ignore[attr-defined]
        timezone=branch.timezone,  # type: ignore[attr-defined]
        created_at=branch.created_at,  # type: ignore[attr-defined]
    )


@router.get(
    "",
    response_model=BranchListResponse,
    summary="List branches",
    dependencies=[_require_manage],
)
async def list_branches(
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    is_active: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> BranchListResponse:
    """Every branch (active and closed), ordered by code."""
    await limiter.enforce(
        f"read:branches:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_read_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    rows, total = await _service(request).list_branches(
        is_active=is_active, limit=limit, offset=offset
    )
    return BranchListResponse(
        items=[_response(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.post(
    "",
    response_model=BranchResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a branch",
    dependencies=[_require_manage],
    responses={
        409: {"description": "Duplicate branch code"},
        422: {"description": "Schema violation"},
    },
)
async def create_branch(
    payload: BranchCreateRequest,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> BranchResponse:
    """Create a branch (audit ``BRANCH_CREATED``)."""
    await limiter.enforce(
        f"write:branches:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_write_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    branch = await _service(request).create_branch(
        code=payload.code,
        name=payload.name,
        address=payload.address,
        phone=payload.phone,
        is_active=payload.is_active,
        timezone=payload.timezone,
        actor=_actor(principal, ip=client_ip, request_id=request_id),
    )
    return _response(branch)


@router.get(
    "/{branch_id}",
    response_model=BranchResponse,
    summary="Read one branch",
    dependencies=[_require_manage],
)
async def get_branch(
    branch_id: uuid.UUID,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
) -> BranchResponse:
    """One branch by id."""
    await limiter.enforce(
        f"read:branches:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_read_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    return _response(await _service(request).get_branch(branch_id))


@router.patch(
    "/{branch_id}",
    response_model=BranchResponse,
    summary="Update a branch (code only while it has no history)",
    dependencies=[_require_manage],
    responses={
        409: {"description": "Code is frozen, duplicate, or the last active branch"},
        422: {"description": "Schema violation"},
    },
)
async def update_branch(
    branch_id: uuid.UUID,
    payload: BranchUpdateRequest,
    request: Request,
    limiter: RateLimiterDep,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> BranchResponse:
    """Update name, address, phone, timezone, active flag (and the code while unused)."""
    await limiter.enforce(
        f"write:branches:{principal.user_id}",
        limit=request.app.state.settings.rate_limit_write_per_min,
        window_seconds=_WINDOW_SECONDS,
    )
    branch = await _service(request).update_branch(
        branch_id=branch_id,
        changes=payload.model_dump(exclude_unset=True),
        actor=_actor(principal, ip=client_ip, request_id=request_id),
    )
    return _response(branch)
