"""FastAPI dependencies: infrastructure handles, authentication and authorisation.

The rule this module implements (SECURITY.md §3, PART 41): **deny by default**. A route
is unprotected only if it takes no principal and no permission dependency; everything
else resolves a :class:`Principal` first, and that resolution re-reads the database on
every request. Consequences that matter:

* **Authorisation is never read from the token.** The token carries roles and a
  permission fingerprint for diagnostics, but the permission set is recomputed from
  ``user_roles``/``role_permissions``/``user_permissions`` (deny wins) for each request.
  Editing a role therefore takes effect on the next call, not after 15 minutes.
* **Revocation is immediate.** Logout, password change, deactivation and device
  revocation all invalidate the session row the access token points at, so a stolen
  access token stops working as soon as its session does.
* **A changed permission set invalidates the token.** ``perm_hash`` mismatch answers
  ``401 TOKEN_INVALID`` with ``details.reason = AUTHORIZATION_CHANGED``: the client signs
  in again instead of continuing with a stale view of its authority.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, cast

from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import Database
from app.core.exceptions import (
    AccountDisabledError,
    AuthenticationError,
    DeviceMismatchError,
    DeviceRevokedError,
    PermissionDeniedError,
    ServiceUnavailableError,
    TokenInvalidError,
    TokenRevokedError,
)
from app.core.logging import get_logger
from app.core.permissions import Permission, permission_hash
from app.core.rate_limit import RateLimiter
from app.core.redis import RedisManager
from app.core.revocation import RevocationList
from app.core.security import normalize_ip_address
from app.core.tokens import AccessTokenClaims, TokenService
from app.models.branch import Branch
from app.models.device import Device
from app.models.security import RefreshToken
from app.models.user import User
from app.repositories.sessions import SessionRepository
from app.repositories.users import UserRepository
from app.services.audit_service import ActorContext
from app.services.auth_service import AuthService, build_auth_service

logger = get_logger(__name__)

# ``auto_error=False`` keeps the 401 in our own hands: the envelope, the request id and
# the error code are part of the contract, so FastAPI must not produce the response.
bearer_scheme = HTTPBearer(auto_error=False)

# Header a client may send to prove which installation it is. When present it must match
# the device the session is bound to (API_CONTRACT §4: DEVICE_MISMATCH).
DEVICE_ID_HEADER = "X-Device-Id"


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated actor of a request, as verified against the database."""

    user: User
    device: Device | None
    branch: Branch | None
    session: RefreshToken
    claims: AccessTokenClaims
    roles: tuple[str, ...]
    permissions: frozenset[str]

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    @property
    def device_id(self) -> uuid.UUID | None:
        return self.device.id if self.device else None

    @property
    def branch_id(self) -> uuid.UUID | None:
        return self.branch.id if self.branch else None

    @property
    def session_id(self) -> uuid.UUID:
        return self.session.family_id

    @property
    def jti(self) -> str:
        return self.claims.jti

    @property
    def access_expires_in_seconds(self) -> int:
        return max(1, int((self.claims.exp - dt.datetime.now(tz=dt.UTC)).total_seconds()))

    @property
    def must_change_password(self) -> bool:
        return bool(self.user.must_change_password)

    def has(self, permission: Permission | str) -> bool:
        return str(permission) in self.permissions


# --------------------------------------------------------------------------- handles
def get_app_settings() -> Settings:
    """Validated settings for the running process."""
    return get_settings()


def _state_handle(request: Request, name: str) -> object | None:
    """Return a lifespan-created handle, or ``None`` when startup did not finish."""
    return getattr(request.app.state, name, None)


def get_database(request: Request) -> Database:
    """The process-wide database handle created in the lifespan.

    A missing handle means the API is running without a usable database (startup
    failed or is still in progress). Answering ``503`` is correct: the caller must
    retry, and for a device in the field that means staying offline rather than
    trading against an unknown ledger state.
    """
    database = _state_handle(request, "database")
    if database is None:
        raise ServiceUnavailableError(
            "The database is not available.",
            details={
                "component": "postgresql",
                "hint": "Retry shortly; check /api/v1/health/ready.",
            },
        )
    return cast(Database, database)


def get_redis(request: Request) -> RedisManager:
    """The process-wide Redis handle created in the lifespan (see ``get_database``)."""
    redis = _state_handle(request, "redis")
    if redis is None:
        raise ServiceUnavailableError(
            "Redis is not available.",
            details={"component": "redis", "hint": "Retry shortly; check /api/v1/health/ready."},
        )
    return cast(RedisManager, redis)


def get_optional_database(request: Request) -> Database | None:
    """Database handle for probes that must answer even when it is unavailable."""
    return cast(Database | None, _state_handle(request, "database"))


def get_optional_redis(request: Request) -> RedisManager | None:
    """Redis handle for probes that must answer even when it is unavailable."""
    return cast(RedisManager | None, _state_handle(request, "redis"))


def get_request_id(request: Request) -> str | None:
    """Correlation id assigned by the request-id middleware."""
    return getattr(request.state, "request_id", None)


def get_client_ip(request: Request) -> str | None:
    """The caller's address as seen through the trusted proxy chain.

    uvicorn applies ``FORWARDED_ALLOW_IPS`` before the request reaches the app, so
    ``request.client.host`` is the real peer unless a trusted proxy said otherwise —
    never a header this code reads itself. The value is normalised to a real address
    because it is persisted to ``INET`` columns (see
    :func:`app.core.security.normalize_ip_address`).
    """
    return normalize_ip_address(request.client.host if request.client else None)


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    """Read-oriented session (no implicit commit; services open transactions)."""
    async with database.session() as session:
        yield session


# ------------------------------------------------------------------------ factories
@lru_cache(maxsize=1)
def get_token_service() -> TokenService:
    """Process-wide token service (JWT keys are read once from validated settings)."""
    return TokenService(get_settings())


def get_rate_limiter(redis: Annotated[RedisManager, Depends(get_redis)]) -> RateLimiter:
    """Rate limiter bound to the process-wide Redis handle."""
    return RateLimiter(redis, get_settings())


def get_revocation_list(redis: Annotated[RedisManager, Depends(get_redis)]) -> RevocationList:
    """Access-token denylist (see :mod:`app.core.revocation`)."""
    return RevocationList(redis)


def get_auth_service(
    database: Annotated[Database, Depends(get_database)],
    revocation: Annotated[RevocationList, Depends(get_revocation_list)],
) -> AuthService:
    """Authentication service (constructed per request; the hasher is cheap)."""
    return build_auth_service(
        database=database,
        settings=get_settings(),
        tokens=get_token_service(),
        revocation_list=revocation,
    )


# ----------------------------------------------------------------------- principal
def _unauthorized(message: str, details: dict[str, object] | None = None) -> AuthenticationError:
    return AuthenticationError(
        message,
        details=details or {"hint": "Send an OAuth2 bearer token in the Authorization header."},
        http_status=status.HTTP_401_UNAUTHORIZED,
    )


async def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    database: Annotated[Database, Depends(get_database)],
    revocation: Annotated[RevocationList, Depends(get_revocation_list)],
    tokens: Annotated[TokenService, Depends(get_token_service)],
) -> Principal:
    """Resolve and verify the caller's session, or raise the contract's 401."""
    if credentials is None or not credentials.credentials:
        raise _unauthorized("Authentication is required for this endpoint.")
    if credentials.scheme.lower() != "bearer":
        raise _unauthorized("Only the Bearer scheme is accepted.")

    claims = tokens.decode_access_token(credentials.credentials)

    if await revocation.is_revoked(claims.jti):
        raise TokenRevokedError(
            "This token has been revoked. Sign in again.",
            details={"reason": "LOGOUT"},
        )

    claimed_device = request.headers.get(DEVICE_ID_HEADER)
    if claimed_device:
        try:
            claimed_device_id: uuid.UUID | None = uuid.UUID(claimed_device)
        except ValueError as exc:
            raise TokenInvalidError(
                "The X-Device-Id header is not a UUID.",
                details={"header": DEVICE_ID_HEADER},
            ) from exc
        if claims.did != claimed_device_id:
            raise DeviceMismatchError(details={"hint": "This token belongs to a different device."})

    async with database.session() as session:
        sessions = SessionRepository(session)
        context = await sessions.load_session_context(claims.sid)
        if context is None:
            raise TokenRevokedError(
                "The session no longer exists. Sign in again.",
                details={"reason": "SESSION_GONE"},
            )
        if context.token.revoked_at is not None:
            raise TokenRevokedError(
                "This session has been revoked. Sign in again.",
                details={"reason": str(context.token.revoked_reason or "REVOKED")},
            )
        if context.token.user_id != claims.sub:
            raise TokenInvalidError("The token does not belong to this session.")
        if context.token.device_id is not None and context.token.device_id != claims.did:
            raise DeviceMismatchError(details={"hint": "The session is bound to another device."})

        user = context.user
        if not user.is_active:
            raise AccountDisabledError()
        device = context.device
        if context.token.device_id is not None:
            if device is None:
                raise DeviceRevokedError(details={"hint": "The device is no longer registered."})
            if device.revoked_at is not None or not device.is_active:
                raise DeviceRevokedError(details={"device_id": str(device.id)})

        users = UserRepository(session)
        roles = tuple(await users.role_names(user.id))
        permissions = await users.effective_permissions(user.id)

    if permission_hash(permissions) != claims.perm_hash:
        # Roles, grants or denies changed after this token was minted.
        raise TokenInvalidError(
            "Your authorisation changed. Sign in again to continue.",
            details={"reason": "AUTHORIZATION_CHANGED"},
        )

    return Principal(
        user=user,
        device=device,
        branch=context.branch,
        session=context.token,
        claims=claims,
        roles=roles,
        permissions=permissions,
    )


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def actor_context(
    principal: Principal, *, ip_address: str | None, request_id: str | None
) -> ActorContext:
    """The audit actor for a request, carrying the caller's real authority.

    ``permissions`` is part of the context on purpose: the administrative services refuse
    to hand out authority the caller does not hold (an escalation guard), and that guard
    is only meaningful if the caller's effective permissions travel with the request.
    """
    return ActorContext(
        user_id=principal.user_id,
        device_id=principal.device_id,
        ip_address=ip_address,
        request_id=request_id,
        permissions=principal.permissions,
        roles=principal.roles,
        branch_id=principal.branch_id,
    )


def require_permission(
    permission: Permission | str,
) -> Callable[[Principal], Awaitable[Principal]]:
    """Dependency factory: authenticated **and** holding ``permission``.

    ``401`` when there is no valid session, ``403 PERMISSION_DENIED`` when the session is
    valid but the permission is missing — the distinction the contract promises and the
    client uses to decide between "sign in" and "ask an administrator".
    """
    required = str(permission)

    async def _dependency(principal: PrincipalDep) -> Principal:
        if not principal.has(required):
            logger.info(
                "permission_denied",
                required_permission=required,
                user_id=str(principal.user_id),
                roles=list(principal.roles),
            )
            raise PermissionDeniedError(
                "You do not have permission to perform this action.",
                details={"required_permission": required},
            )
        if principal.must_change_password:
            # A freshly created (or reset) account may authenticate but not exercise
            # authority until it has chosen its own password.
            raise PermissionDeniedError(
                "Change your password before using this account.",
                details={
                    "reason": "PASSWORD_CHANGE_REQUIRED",
                    "allowed_endpoints": [
                        "/api/v1/auth/password",
                        "/api/v1/auth/logout",
                        "/api/v1/auth/me",
                    ],
                },
            )
        return principal

    return _dependency


def require_any_permission(
    *permissions: Permission | str,
) -> Callable[[Principal], Awaitable[Principal]]:
    """Authenticated and holding at least one of ``permissions`` (deny otherwise)."""
    required = {str(permission) for permission in permissions}

    async def _dependency(principal: PrincipalDep) -> Principal:
        if not (required & set(principal.permissions)):
            raise PermissionDeniedError(
                "You do not have permission to perform this action.",
                details={"required_any_of": sorted(required)},
            )
        if principal.must_change_password:
            raise PermissionDeniedError(
                "Change your password before using this account.",
                details={
                    "reason": "PASSWORD_CHANGE_REQUIRED",
                    "allowed_endpoints": [
                        "/api/v1/auth/password",
                        "/api/v1/auth/logout",
                        "/api/v1/auth/me",
                    ],
                },
            )
        return principal

    return _dependency


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]
RedisDep = Annotated[RedisManager, Depends(get_redis)]
RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
RevocationDep = Annotated[RevocationList, Depends(get_revocation_list)]
TokenServiceDep = Annotated[TokenService, Depends(get_token_service)]
ClientIpDep = Annotated[str | None, Depends(get_client_ip)]
# These two must use a real type (not a quoted forward reference): FastAPI reads the
# first argument of Annotated to decide whether a parameter is a dependency or a
# request field, and a string literal makes it fall back to a query parameter.
OptionalDatabaseDep = Annotated[Database | None, Depends(get_optional_database)]
OptionalRedisDep = Annotated[RedisManager | None, Depends(get_optional_redis)]
RequestIdDep = Annotated[str | None, Depends(get_request_id)]
