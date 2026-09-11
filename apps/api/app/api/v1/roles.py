"""Role and permission catalogue (API_CONTRACT §9.1).

Read endpoints let an administrator UI render the matrix; the write endpoint replaces a
role's permission set with an audited diff. ``SUPER_ADMIN`` is a system role and editing
it is refused — the role that administers the system must not be silently denuded.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request

from app.api.deps import (
    ClientIpDep,
    PrincipalDep,
    RequestIdDep,
    actor_context,
    require_permission,
)
from app.core.exceptions import ResourceNotFoundError
from app.core.permissions import SYSTEM_ROLES, Permission
from app.repositories.users import RoleRepository
from app.schemas.users import (
    PermissionListResponse,
    PermissionResponse,
    RoleListResponse,
    RolePermissionUpdateRequest,
    RoleResponse,
)
from app.services.user_service import build_user_service

router = APIRouter(tags=["authorisation"])

_require_users_manage = Depends(require_permission(Permission.USERS_MANAGE))


def _role_response(role: object, permissions: frozenset[str]) -> RoleResponse:
    return RoleResponse(
        id=role.id,  # type: ignore[attr-defined]
        name=str(role.name),  # type: ignore[attr-defined]
        description=role.description,  # type: ignore[attr-defined]
        is_system=bool(role.is_system),  # type: ignore[attr-defined]
        is_editable=str(role.name) not in {str(name) for name in SYSTEM_ROLES},  # type: ignore[attr-defined]
        permissions=sorted(permissions),
    )


@router.get(
    "/roles",
    response_model=RoleListResponse,
    summary="List roles with their permission sets",
    dependencies=[_require_users_manage],
)
async def list_roles(request: Request) -> RoleListResponse:
    async with request.app.state.database.session() as session:
        repo = RoleRepository(session)
        roles = await repo.list_roles()
        items = [_role_response(role, await repo.role_permission_codes(role.id)) for role in roles]
    return RoleListResponse(items=items, total=len(items))


@router.get(
    "/permissions",
    response_model=PermissionListResponse,
    summary="List the permission catalogue",
    dependencies=[_require_users_manage],
)
async def list_permissions(request: Request) -> PermissionListResponse:
    async with request.app.state.database.session() as session:
        rows = await RoleRepository(session).list_permissions()
    items = [PermissionResponse(code=str(row.code), description=row.description) for row in rows]
    return PermissionListResponse(items=items, total=len(items))


@router.post(
    "/roles/{role_id}/permissions",
    response_model=RoleResponse,
    summary="Replace a role's permission set",
    dependencies=[_require_users_manage],
    responses={
        403: {"description": "System role, or an attempt to grant authority the caller lacks"},
        404: {"description": "Unknown role"},
    },
)
async def replace_role_permissions(
    role_id: uuid.UUID,
    payload: RolePermissionUpdateRequest,
    request: Request,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> RoleResponse:
    """Replace the grants of a role with exactly the supplied set (audited diff)."""
    service = build_user_service(
        database=request.app.state.database, settings=request.app.state.settings
    )
    await service.replace_role_permissions(
        role_id=role_id,
        permission_codes=payload.permissions,
        actor=actor_context(principal, ip_address=client_ip, request_id=request_id),
    )
    async with request.app.state.database.session() as session:
        repo = RoleRepository(session)
        stored = await repo.get_role(role_id)
        if stored is None:  # pragma: no cover - deleted between the two reads (impossible)
            raise ResourceNotFoundError("No such role.")
        permissions = await repo.role_permission_codes(role_id)
    return _role_response(stored, permissions)


__all__ = ["router"]
