"""User administration (API_CONTRACT §9.1): users, role assignment, permission overrides.

Every endpoint requires ``users.manage``; the service enforces the escalation guards
(a caller cannot grant authority they do not hold, cannot deactivate themselves, cannot
strip their own user-management right). ``DELETE`` is a documented soft delete — the
account is deactivated, its sessions are revoked, and the row stays in the database
forever (PART 25).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.deps import (
    ClientIpDep,
    PrincipalDep,
    RequestIdDep,
    actor_context,
    require_permission,
)
from app.core.permissions import Permission
from app.schemas.users import (
    PermissionOverridesRequest,
    UserCreateRequest,
    UserListResponse,
    UserPermissionState,
    UserResponse,
    UserUpdateRequest,
)
from app.services.audit_service import ActorContext
from app.services.user_service import (
    PermissionOverride,
    UserProfile,
    UserService,
    build_user_service,
)

router = APIRouter(prefix="/users", tags=["users"])

# ``Query(...)`` in a default argument is FastAPI's documented style (B008 is a false positive
# for dependency-injection frameworks): the call is evaluated once, at import time.

# ``users.manage`` guards the whole router; the dependency raises 401 or 403 per the
# contract, and refuses any call while the caller must change their password.
_require_users_manage = Depends(require_permission(Permission.USERS_MANAGE))


def _service(request: Request) -> UserService:
    return build_user_service(
        database=request.app.state.database, settings=request.app.state.settings
    )


def _actor(principal: PrincipalDep, *, ip: str | None, request_id: str | None) -> ActorContext:
    """Audit context for this request (see :func:`app.api.deps.actor_context`)."""
    return actor_context(principal, ip_address=ip, request_id=request_id)


def _user_response(profile: UserProfile) -> UserResponse:
    """Serialize a profile; ``password_hash`` is not even part of the schema."""
    return UserResponse(
        id=profile.user.id,
        username=profile.user.username,
        full_name=profile.user.full_name,
        email=profile.user.email,
        phone=profile.user.phone,
        is_active=profile.user.is_active,
        must_change_password=profile.user.must_change_password,
        failed_login_attempts=profile.user.failed_login_attempts,
        locked_until=profile.user.locked_until,
        last_login_at=profile.user.last_login_at,
        password_changed_at=profile.user.password_changed_at,
        created_at=profile.user.created_at,
        updated_at=profile.user.updated_at,
        roles=sorted(profile.roles),
        permissions=sorted(profile.permissions),
        overrides=[
            UserPermissionState(
                permission_code=str(override["permission_code"]),
                is_granted=bool(override["is_granted"]),
                expires_at=override.get("expires_at"),  # type: ignore[arg-type]
                reason=override.get("reason"),  # type: ignore[arg-type]
            )
            for override in profile.overrides
        ],
    )


@router.get(
    "",
    response_model=UserListResponse,
    summary="List users",
    dependencies=[_require_users_manage],
)
async def list_users(
    request: Request,
    is_active: bool | None = Query(default=None),
    role: str | None = Query(default=None, description="Filter by role name"),
    branch_id: uuid.UUID | None = Query(
        default=None, description="Users with a device registered in this branch"
    ),
    search: str | None = Query(default=None, max_length=100, description="username/name/e-mail"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> UserListResponse:
    """Paginated user list (part of the standard list envelope)."""
    profiles, total = await _service(request).list_users(
        is_active=is_active,
        role_name=role,
        branch_id=branch_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    return UserListResponse(
        items=[_user_response(profile) for profile in profiles],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user",
    dependencies=[_require_users_manage],
    responses={409: {"description": "Username or e-mail already exists"}},
)
async def create_user(
    payload: UserCreateRequest,
    request: Request,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> UserResponse:
    """Create an account; the password policy is enforced before hashing."""
    created = await _service(request).create_user(
        username=payload.username,
        password=payload.password.get_secret_value(),
        full_name=payload.full_name,
        email=str(payload.email) if payload.email else None,
        phone=payload.phone,
        role_names=payload.roles,
        must_change_password=payload.must_change_password,
        actor=_actor(principal, ip=client_ip, request_id=request_id),
    )
    profile = await _service(request).get_profile(created.user.id)
    return _user_response(profile)


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    summary="Get a user",
    dependencies=[_require_users_manage],
    responses={404: {"description": "Unknown user"}},
)
async def get_user(user_id: uuid.UUID, request: Request) -> UserResponse:
    return _user_response(await _service(request).get_profile(user_id))


@router.patch(
    "/{user_id}",
    response_model=UserResponse,
    summary="Update a user (profile, state, roles)",
    dependencies=[_require_users_manage],
)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateRequest,
    request: Request,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> UserResponse:
    """Partial update. Deactivating a user revokes their sessions in the same transaction."""
    changes = payload.model_dump(exclude_unset=True, exclude={"roles"})
    if changes.get("email") is not None:
        changes["email"] = str(changes["email"])
    profile = await _service(request).update_user(
        user_id=user_id,
        changes=changes,
        role_names=payload.roles,
        actor=_actor(principal, ip=client_ip, request_id=request_id),
    )
    return _user_response(profile)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
    summary="Deactivate a user (soft delete — the row is never removed)",
    dependencies=[_require_users_manage],
)
async def deactivate_user(
    user_id: uuid.UUID,
    request: Request,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> None:
    """PART 25: deactivation instead of deletion; the account keeps its history."""
    await _service(request).deactivate_user(
        user_id=user_id,
        actor=_actor(principal, ip=client_ip, request_id=request_id),
    )


@router.put(
    "/{user_id}/permissions",
    response_model=UserResponse,
    summary="Replace a user's explicit grants and denies",
    dependencies=[_require_users_manage],
    responses={403: {"description": "Attempt to grant authority the caller does not hold"}},
)
async def set_permissions(
    user_id: uuid.UUID,
    payload: PermissionOverridesRequest,
    request: Request,
    principal: PrincipalDep,
    client_ip: ClientIpDep,
    request_id: RequestIdDep,
) -> UserResponse:
    """Set the whole override list; an explicit deny beats any role grant."""
    profile = await _service(request).set_permission_overrides(
        user_id=user_id,
        overrides=[
            PermissionOverride(
                permission_code=override.permission_code,
                is_granted=override.is_granted,
                expires_at=override.expires_at,
                reason=override.reason,
            )
            for override in payload.overrides
        ],
        actor=_actor(principal, ip=client_ip, request_id=request_id),
    )
    return _user_response(profile)


__all__ = ["router"]
