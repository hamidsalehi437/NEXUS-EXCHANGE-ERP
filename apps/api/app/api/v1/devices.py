"""Device administration (API_CONTRACT §9.1).

``POST /devices/register`` needs ``device.register`` (a manager or cashier provisioning a
counter); listing and revoking need ``device.manage`` (administrators). Revocation is the
emergency control of this phase: one call flags the device, ends its sessions and records
who did it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.deps import (
    ClientIpDep,
    PrincipalDep,
    RequestIdDep,
    actor_context,
    require_permission,
)
from app.core.permissions import Permission
from app.models.device import Device
from app.schemas.devices import (
    DeviceListResponse,
    DeviceRegisterRequest,
    DeviceResponse,
    DeviceRevokeRequest,
    DeviceRevokeResponse,
)
from app.services.audit_service import ActorContext
from app.services.device_service import DeviceService

router = APIRouter(prefix="/devices", tags=["devices"])

# See the note in app/api/v1/users.py about B008 (FastAPI's Query-in-default idiom).

_register = Depends(require_permission(Permission.DEVICE_REGISTER))
_manage = Depends(require_permission(Permission.DEVICE_MANAGE))


def _service(request: Request) -> DeviceService:
    return DeviceService(database=request.app.state.database)


def _actor(principal: PrincipalDep, *, ip: str | None, request_id: str | None) -> ActorContext:
    """Audit context for this request (see :func:`app.api.deps.actor_context`)."""
    return actor_context(principal, ip_address=ip, request_id=request_id)


def _device_response(device: Device) -> DeviceResponse:
    return DeviceResponse(
        id=device.id,
        device_uuid=device.device_uuid,
        device_name=device.device_name,
        platform=device.platform,
        branch_id=device.branch_id,
        is_active=device.is_active,
        app_version=device.app_version,
        registered_by=device.registered_by,
        last_seen_at=device.last_seen_at,
        last_sync_at=device.last_sync_at,
        created_at=device.created_at,
        revoked_at=device.revoked_at,
        revoked_by=device.revoked_by,
        revoke_reason=device.revoke_reason,
    )


@router.get(
    "",
    response_model=DeviceListResponse,
    summary="List registered devices",
    dependencies=[_manage],
)
async def list_devices(
    request: Request,
    branch_id: uuid.UUID | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> DeviceListResponse:
    devices, total = await _service(request).list_devices(
        branch_id=branch_id, is_active=is_active, limit=limit, offset=offset
    )
    return DeviceListResponse(
        items=[_device_response(device) for device in devices],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/register",
    response_model=DeviceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a device to a branch",
    dependencies=[_register],
    responses={409: {"description": "This device identifier is already registered"}},
)
async def register_device(
    payload: DeviceRegisterRequest,
    request: Request,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> DeviceResponse:
    """Provision a device for a branch (setup path; self-registration happens at login)."""
    registration = await _service(request).register_device(
        device_uuid=payload.device_uuid,
        device_name=payload.device_name,
        platform=payload.platform,
        branch_id=payload.branch_id,
        app_version=payload.app_version,
        actor=_actor(principal, ip=client_ip, request_id=request_id),
    )
    return _device_response(registration.device)


@router.post(
    "/{device_id}/revoke",
    response_model=DeviceRevokeResponse,
    summary="Revoke a device (ends its sessions immediately)",
    dependencies=[_manage],
    responses={404: {"description": "Unknown device"}},
)
async def revoke_device(
    device_id: uuid.UUID,
    payload: DeviceRevokeRequest,
    request: Request,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> DeviceRevokeResponse:
    """Revoke a device: flag it, kill its sessions, audit the act. Idempotent."""
    result = await _service(request).revoke_device(
        device_id=device_id,
        reason=payload.reason,
        actor=_actor(principal, ip=client_ip, request_id=request_id),
    )
    return DeviceRevokeResponse(
        device=_device_response(result.device),
        revoked_sessions=result.revoked_sessions,
        already_revoked=result.already_revoked,
    )


__all__ = ["router"]
