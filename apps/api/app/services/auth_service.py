"""Authentication service — login, refresh rotation, logout, sessions, password change.

Security properties this module is responsible for (SECURITY.md §2, PART 24/42):

* passwords are verified with Argon2id and transparently re-hashed when the configured
  parameters change; the plaintext is never stored, compared, logged or audited;
* an unknown username and a wrong password are indistinguishable (same status, same
  code, same message, and a dummy Argon2 verification so the response time matches);
* refresh tokens rotate on every exchange, and replaying an exchanged token revokes the
  whole family with a ``SECURITY_REFRESH_REUSE_DETECTED`` audit entry;
* sessions are bound to a registered device; revocation (logout, password change,
  device revocation, account deactivation) takes effect on the *next request*, including
  for access tokens, because every request re-reads the session from the database;
* every outcome — including refusals — is audited, and the audit row commits even when
  the request itself fails.

A refusal that must still be recorded is returned as an error **object** and raised after
the surrounding transaction commits. That is why several private methods return
``NexusError | None`` instead of raising: the 401 and its audit entry have to be produced
in the same unit of work.
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from dataclasses import dataclass, replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_actions import AuditAction
from app.core.config import Settings
from app.core.database import Database
from app.core.exceptions import (
    AccountDisabledError,
    AccountLockedError,
    AuthenticationError,
    DeviceMismatchError,
    DeviceRevokedError,
    DeviceUnknownError,
    InvalidCredentialsError,
    NexusError,
    ResourceNotFoundError,
    TokenExpiredError,
    TokenInvalidError,
    TokenRevokedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.core.revocation import RevocationList
from app.core.security import (
    PasswordHasher,
    PasswordPolicyError,
    build_password_hasher,
    enforce_password_policy,
)
from app.core.tokens import IssuedAccessToken, TokenService
from app.models.branch import Branch
from app.models.device import Device
from app.models.security import RefreshToken
from app.models.user import User
from app.repositories.devices import DeviceRepository
from app.repositories.sessions import (
    DELIBERATE_REVOCATION_REASONS,
    REASON_EXPIRED,
    REASON_LOGOUT,
    REASON_LOGOUT_ALL,
    REASON_PASSWORD_CHANGED,
    REASON_REUSE_DETECTED,
    REASON_SESSION_REVOKED,
    SessionRepository,
    SessionSummary,
)
from app.repositories.users import BranchRepository, UserRepository
from app.services.audit_service import ActorContext, AuditService

logger = get_logger(__name__)

# Random per-process value used only to equalise the cost of a login attempt for a
# username that does not exist (SECURITY.md §2: no user enumeration).
_DUMMY_PASSWORD = secrets.token_urlsafe(48)


@dataclass(frozen=True, slots=True)
class LoginRequestData:
    """Validated login input (the route turns the HTTP body into this)."""

    username: str
    password: str
    device_uuid: uuid.UUID
    device_name: str
    platform: str
    app_version: str | None = None
    branch_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """Everything the login/refresh response needs, with the credentials still valid."""

    access: IssuedAccessToken
    refresh_token: str
    refresh_expires_in: int
    user: User
    device: Device
    branch: Branch | None
    roles: list[str]
    permissions: frozenset[str]
    session_id: uuid.UUID
    is_new_device: bool


@dataclass(frozen=True, slots=True)
class LogoutResult:
    """What a logout revoked."""

    scope: str
    revoked_sessions: int
    session_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class PasswordChangeResult:
    """Outcome of a password change."""

    password_changed_at: dt.datetime
    revoked_sessions: int


class AuthService:
    """Authenticate users and manage their sessions."""

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        tokens: TokenService,
        hasher: PasswordHasher,
        revocation_list: RevocationList,
    ) -> None:
        self._database = database
        self._settings = settings
        self._tokens = tokens
        self._hasher = hasher
        self._revocation = revocation_list
        self._dummy_hash: str | None = None

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _now() -> dt.datetime:
        return dt.datetime.now(tz=dt.UTC)

    def _refresh_expiry(self, now: dt.datetime) -> dt.datetime:
        return now + dt.timedelta(days=self._settings.refresh_token_expire_days)

    def _dummy_password_hash(self) -> str:
        """Argon2id hash of a random value, computed once per process."""
        if self._dummy_hash is None:
            self._dummy_hash = self._hasher.hash(_DUMMY_PASSWORD)
        return self._dummy_hash

    async def _issue_access(
        self,
        *,
        user: User,
        device: Device,
        branch: Branch | None,
        family_id: uuid.UUID,
        roles: list[str],
        permissions: frozenset[str],
    ) -> IssuedAccessToken:
        return self._tokens.issue_access_token(
            user_id=user.id,
            session_id=family_id,
            device_id=device.id,
            branch_id=branch.id if branch else None,
            roles=roles,
            permissions=permissions,
        )

    # -------------------------------------------------------------------- login
    async def login(self, data: LoginRequestData, *, actor: ActorContext) -> AuthenticatedSession:
        """Verify credentials, bind a device and open a session."""
        failure: NexusError | None = None
        result: AuthenticatedSession | None = None

        async with self._database.transaction() as session:
            users = UserRepository(session)
            devices = DeviceRepository(session)
            session_repo = SessionRepository(session)
            audit = AuditService(session)
            now = self._now()

            user = await users.get_by_username(data.username, for_update=True)
            if user is None:
                # Constant-cost rejection: verify against a throwaway hash so an unknown
                # username takes the same time as a wrong password.
                self._hasher.verify(data.password, self._dummy_password_hash())
                audit.record(
                    action=AuditAction.AUTH_LOGIN_FAILED,
                    entity_type="user",
                    new_data={"username": data.username, "reason": "UNKNOWN_USER"},
                    actor=ActorContext(ip_address=actor.ip_address, request_id=actor.request_id),
                )
                failure = InvalidCredentialsError()
            else:
                decision = await self._authenticate_existing_user(
                    session, users, audit, user=user, data=data, actor=actor, now=now
                )
                if isinstance(decision, NexusError):
                    failure = decision
                else:
                    device, is_new_device = decision
                    roles = await users.role_names(user.id)
                    permissions = await users.effective_permissions(user.id)
                    result = await self._open_session(
                        session,
                        devices=devices,
                        session_repo=session_repo,
                        audit=audit,
                        user=user,
                        device=device,
                        roles=roles,
                        permissions=permissions,
                        is_new_device=is_new_device,
                        data=data,
                        actor=actor,
                        now=now,
                    )

        if failure is not None:
            raise failure
        if result is None:  # pragma: no cover - defensive
            raise AuthenticationError("Login did not complete.")
        return result

    async def _authenticate_existing_user(
        self,
        session: AsyncSession,
        users: UserRepository,
        audit: AuditService,
        *,
        user: User,
        data: LoginRequestData,
        actor: ActorContext,
        now: dt.datetime,
    ) -> tuple[Device, bool] | NexusError:
        """Check password, account state and lockout state; then resolve the device."""
        verification = self._hasher.verify(data.password, user.password_hash)
        if not verification.is_valid:
            return await self._register_failed_login(audit, user=user, actor=actor, now=now)

        if not user.is_active:
            audit.record(
                action=AuditAction.AUTH_LOGIN_DENIED,
                entity_type="user",
                entity_id=user.id,
                new_data={"username": user.username, "reason": "ACCOUNT_INACTIVE"},
                actor=actor,
            )
            return AccountDisabledError()

        if user.locked_until is not None and user.locked_until > now:
            audit.record(
                action=AuditAction.AUTH_LOGIN_DENIED,
                entity_type="user",
                entity_id=user.id,
                new_data={
                    "username": user.username,
                    "reason": "ACCOUNT_LOCKED",
                    "locked_until": user.locked_until.isoformat(),
                },
                actor=actor,
            )
            return AccountLockedError(details={"locked_until": user.locked_until.isoformat()})

        # A correct password after an expired lock clears the counter (the account is
        # usable again without an administrator's help).
        if user.locked_until is not None or user.failed_login_attempts:
            user.failed_login_attempts = 0
            user.locked_until = None

        resolved = await self._resolve_device(
            session,
            users,
            DeviceRepository(session),
            audit,
            user=user,
            data=data,
            actor=actor,
            now=now,
        )
        if isinstance(resolved, NexusError):
            return resolved
        device, is_new_device = resolved
        if verification.needs_rehash:
            user.password_hash = self._hasher.hash(data.password)
            audit.record(
                action=AuditAction.AUTH_CREDENTIAL_UPGRADED,
                entity_type="user",
                entity_id=user.id,
                new_data={"argon2id": self._hasher.parameters},
                actor=actor,
            )
        return device, is_new_device

    async def _open_session(
        self,
        session: AsyncSession,
        *,
        devices: DeviceRepository,
        session_repo: SessionRepository,
        audit: AuditService,
        user: User,
        device: Device,
        roles: list[str],
        permissions: frozenset[str],
        is_new_device: bool,
        data: LoginRequestData,
        actor: ActorContext,
        now: dt.datetime,
    ) -> AuthenticatedSession:
        """Open a refresh family and mint the first access token for it."""
        minted = self._tokens.mint_refresh_token()
        token_row = await session_repo.create_family(
            user_id=user.id,
            device_id=device.id,
            token_hash=minted.token_hash,
            expires_at=self._refresh_expiry(now),
            ip_address=actor.ip_address,
            user_agent=data.app_version,
            issued_at=now,
        )
        await session.flush()
        branch = await devices.branch_of(device.id)
        access = await self._issue_access(
            user=user,
            device=device,
            branch=branch,
            family_id=token_row.family_id,
            roles=roles,
            permissions=permissions,
        )

        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = now
        device.last_seen_at = now
        audit.record(
            action=AuditAction.AUTH_LOGIN_SUCCEEDED,
            entity_type="user",
            entity_id=user.id,
            # Attribution rule: an event is recorded with an actor as soon as the
            # credentials have been verified (a session is being opened here); events
            # before that — a wrong password, an unknown username, a locked account —
            # keep a NULL actor and name the account in entity_id and the payload.
            actor=replace(
                actor,
                user_id=user.id,
                device_id=device.id,
                permissions=permissions,
                roles=tuple(roles),
            ),
            new_data={
                "username": user.username,
                "roles": roles,
                "session_id": str(token_row.family_id),
                "device_id": str(device.id),
                "branch_id": str(device.branch_id),
                "platform": device.platform,
                "is_new_device": is_new_device,
                "must_change_password": user.must_change_password,
                "access_expires_at": access.expires_at.isoformat(),
                "app_version": data.app_version,
            },
        )
        return AuthenticatedSession(
            access=access,
            refresh_token=minted.raw,
            refresh_expires_in=self._tokens.refresh_token_ttl_seconds,
            user=user,
            device=device,
            branch=branch,
            roles=roles,
            permissions=permissions,
            session_id=token_row.family_id,
            is_new_device=is_new_device,
        )

    async def _register_failed_login(
        self, audit: AuditService, *, user: User, actor: ActorContext, now: dt.datetime
    ) -> NexusError:
        """Count a failed attempt and lock the account when the threshold is reached.

        A *wrong* password always answers ``401 INVALID_CREDENTIALS`` — even while the
        account is locked — so an attacker cannot use the lockout to confirm that a
        username exists. The locked state itself is reported (``423``) only when the
        password was correct, which is exactly what a legitimate operator needs to see.
        """
        attempts = user.failed_login_attempts + 1
        user.failed_login_attempts = attempts

        if attempts >= self._settings.login_max_failed_attempts:
            user.locked_until = now + dt.timedelta(minutes=self._settings.login_lockout_minutes)
            audit.record(
                action=AuditAction.AUTH_LOCKOUT,
                entity_type="user",
                entity_id=user.id,
                new_data={
                    "username": user.username,
                    "failed_attempts": attempts,
                    "locked_until": user.locked_until.isoformat(),
                    "lockout_minutes": self._settings.login_lockout_minutes,
                },
                actor=actor,
            )
            logger.warning("account_locked", username=user.username, failed_attempts=attempts)
        else:
            audit.record(
                action=AuditAction.AUTH_LOGIN_FAILED,
                entity_type="user",
                entity_id=user.id,
                new_data={
                    "username": user.username,
                    "reason": "INVALID_PASSWORD",
                    "failed_attempts": attempts,
                    "remaining_attempts": max(
                        self._settings.login_max_failed_attempts - attempts, 0
                    ),
                },
                actor=actor,
            )
        return InvalidCredentialsError()

    async def _resolve_device(
        self,
        session: AsyncSession,
        users: UserRepository,
        devices: DeviceRepository,
        audit: AuditService,
        *,
        user: User,
        data: LoginRequestData,
        actor: ActorContext,
        now: dt.datetime,
    ) -> tuple[Device, bool] | NexusError:
        """Find the caller's device, or register it when policy allows.

        An unknown device is accepted only for a user who holds ``device.register``
        (managers and cashiers in the seeded matrix): that is the documented branch
        device auto-registration policy. Everyone else receives ``401 DEVICE_UNKNOWN``.
        """
        device = await devices.get_by_uuid(data.device_uuid, for_update=True)
        if device is not None:
            if device.revoked_at is not None or not device.is_active:
                audit.record(
                    action=AuditAction.AUTH_LOGIN_DENIED,
                    entity_type="device",
                    entity_id=device.id,
                    new_data={
                        "username": user.username,
                        "reason": "DEVICE_REVOKED",
                        "device_uuid": str(data.device_uuid),
                    },
                    actor=actor,
                )
                return DeviceRevokedError(
                    details={
                        "device_id": str(device.id),
                        "hint": "Ask an administrator to re-register this device.",
                    }
                )
            return device, False

        permissions = await users.effective_permissions(user.id)
        if Permission.DEVICE_REGISTER.value not in permissions:
            audit.record(
                action=AuditAction.DEVICE_REGISTRATION_DENIED,
                entity_type="device",
                new_data={
                    "username": user.username,
                    "reason": "MISSING_PERMISSION",
                    "required_permission": Permission.DEVICE_REGISTER.value,
                    "device_uuid": str(data.device_uuid),
                },
                actor=actor,
            )
            return DeviceUnknownError(
                details={
                    "hint": "Register the device first "
                    f"(requires {Permission.DEVICE_REGISTER.value})."
                }
            )

        branch = await self._resolve_branch(session, data.branch_id)
        if isinstance(branch, NexusError):
            return branch

        new_device = Device(
            branch_id=branch.id,
            device_uuid=data.device_uuid,
            device_name=data.device_name or f"Device {str(data.device_uuid)[:8]}",
            platform=data.platform or "WEB",
            app_version=data.app_version,
            registered_by=user.id,
            last_seen_at=now,
            is_active=True,
        )
        devices.add(new_device)
        await session.flush()
        audit.record(
            action=AuditAction.DEVICE_REGISTERED,
            entity_type="device",
            entity_id=new_device.id,
            new_data={
                "device_uuid": str(new_device.device_uuid),
                "device_name": new_device.device_name,
                "platform": new_device.platform,
                "branch_id": str(branch.id),
                "branch_code": branch.code,
                "registered_by_username": user.username,
                "registration_path": "self_service_login",
            },
            # The credentials were verified before this point, so the row names both the
            # account and the installation it belongs to.
            actor=replace(actor, user_id=user.id, device_id=new_device.id),
        )
        return new_device, True

    async def _resolve_branch(
        self, session: AsyncSession, branch_id: uuid.UUID | None
    ) -> Branch | NexusError:
        """Resolve the branch a new device belongs to.

        An explicit ``branch_id`` wins; otherwise a single-branch deployment resolves
        itself. With several branches configured the caller must say which one —
        guessing would file the device (and its cash) under the wrong branch.
        """
        branches = BranchRepository(session)
        if branch_id is not None:
            branch = await branches.get_branch(branch_id)
            if branch is None:
                return ValidationError(
                    "The requested branch does not exist.",
                    details={"fields": [{"field": "branch_id", "code": "not_found"}]},
                )
            if not branch.is_active:
                return ValidationError(
                    "The requested branch is not active.",
                    details={"fields": [{"field": "branch_id", "code": "inactive"}]},
                )
            return branch

        active = await branches.active_branches()
        if len(active) == 1:
            return active[0]
        if not active:
            return ValidationError(
                "No active branch exists; an administrator must create one before "
                "devices register.",
                details={"fields": [{"field": "branch_id", "code": "required"}]},
            )
        return ValidationError(
            "Several branches exist; branch_id is required to register this device.",
            details={"fields": [{"field": "branch_id", "code": "required"}]},
        )

    # ------------------------------------------------------------------ refresh
    async def refresh(
        self,
        *,
        refresh_token: str,
        device_uuid: uuid.UUID | None,
        actor: ActorContext,
    ) -> AuthenticatedSession:
        """Rotate a refresh token, or detect that it is being replayed."""
        self._tokens.verify_refresh_token(refresh_token)
        token_hash = self._tokens.hash_refresh_token(refresh_token)

        failure: NexusError | None = None
        result: AuthenticatedSession | None = None

        async with self._database.transaction() as session:
            users = UserRepository(session)
            devices = DeviceRepository(session)
            session_repo = SessionRepository(session)
            audit = AuditService(session)
            now = self._now()

            token = await session_repo.get_by_hash(token_hash, for_update=True)
            if token is None:
                audit.record(
                    action=AuditAction.AUTH_REFRESH_FAILED,
                    entity_type="refresh_token",
                    new_data={"reason": "UNKNOWN_TOKEN"},
                    actor=actor,
                )
                failure = TokenInvalidError("The refresh token is not valid.")
            else:
                user = await users.get_by_id(token.user_id)
                device = await devices.get_device(token.device_id) if token.device_id else None
                failure = await self._evaluate_refresh_token(
                    session_repo, audit, token=token, user=user, device=device, now=now, actor=actor
                )
                if failure is None and user is not None and device is not None:
                    failure = self._check_device_binding(device, device_uuid)
                if failure is None and user is not None and device is not None:
                    roles = await users.role_names(user.id)
                    permissions = await users.effective_permissions(user.id)
                    minted = self._tokens.mint_refresh_token()
                    successor = await session_repo.create_family(
                        user_id=user.id,
                        device_id=device.id,
                        token_hash=minted.token_hash,
                        expires_at=self._refresh_expiry(now),
                        ip_address=actor.ip_address,
                        user_agent=device.device_name,
                        issued_at=now,
                        family_id=token.family_id,
                        parent_id=token.id,
                    )
                    await session.flush()
                    await session_repo.mark_rotated(token, successor_id=successor.id, used_at=now)
                    branch = await devices.branch_of(device.id)
                    access = await self._issue_access(
                        user=user,
                        device=device,
                        branch=branch,
                        family_id=token.family_id,
                        roles=roles,
                        permissions=permissions,
                    )
                    device.last_seen_at = now
                    audit.record(
                        action=AuditAction.AUTH_REFRESH_ROTATED,
                        entity_type="refresh_token",
                        entity_id=successor.id,
                        new_data={
                            "session_id": str(token.family_id),
                            "device_id": str(device.id),
                            "successor_token_id": str(successor.id),
                            "rotation_depth": await session_repo.count_family_tokens(
                                token.family_id
                            ),
                        },
                        actor=actor,
                    )
                    result = AuthenticatedSession(
                        access=access,
                        refresh_token=minted.raw,
                        refresh_expires_in=self._tokens.refresh_token_ttl_seconds,
                        user=user,
                        device=device,
                        branch=branch,
                        roles=roles,
                        permissions=permissions,
                        session_id=token.family_id,
                        is_new_device=False,
                    )

        if failure is not None:
            raise failure
        if result is None:  # pragma: no cover - defensive
            raise AuthenticationError("Refresh did not complete.")
        return result

    @staticmethod
    def _check_device_binding(
        device: Device, claimed_device_uuid: uuid.UUID | None
    ) -> NexusError | None:
        """A client that names a different device than the session's is rejected."""
        if claimed_device_uuid is not None and device.device_uuid != claimed_device_uuid:
            return DeviceMismatchError(
                details={"hint": "This refresh token belongs to another device."}
            )
        return None

    async def _evaluate_refresh_token(
        self,
        session_repo: SessionRepository,
        audit: AuditService,
        *,
        token: RefreshToken,
        user: User | None,
        device: Device | None,
        now: dt.datetime,
        actor: ActorContext,
    ) -> NexusError | None:
        """Decide whether a presented refresh token may be exchanged.

        Replaying an *exchanged* token is treated as theft: the family is revoked, the
        event is audited, and the holder must sign in again. Replaying a token whose
        session was deliberately ended (logout, password change, device revocation) is a
        plain 401 — there is nothing left to escalate.
        """
        if token.revoked_at is not None:
            if token.revoked_reason == REASON_EXPIRED:
                return TokenExpiredError("The refresh token has expired.")
            if token.revoked_reason in DELIBERATE_REVOCATION_REASONS:
                return TokenRevokedError("This session has been revoked. Sign in again.")
            await session_repo.revoke_family(
                token.family_id, reason=REASON_REUSE_DETECTED, when=now
            )
            audit.record(
                action=AuditAction.SECURITY_REFRESH_REUSE_DETECTED,
                entity_type="refresh_token",
                entity_id=token.id,
                new_data={
                    "session_id": str(token.family_id),
                    "device_id": str(token.device_id) if token.device_id else None,
                    "previous_revocation_reason": token.revoked_reason,
                    "response": "SESSION_REVOKED",
                },
                actor=actor,
            )
            logger.warning(
                "refresh_token_reuse_detected",
                session_id=str(token.family_id),
                token_id=str(token.id),
            )
            return TokenRevokedError(
                "This refresh token was already used; the session has been revoked.",
                details={"reason": "REUSE_DETECTED"},
            )

        if token.expires_at <= now:
            return TokenExpiredError("The refresh token has expired.")
        if user is None:
            return TokenInvalidError("The session owner no longer exists.")
        if not user.is_active:
            return AccountDisabledError()
        if token.device_id is not None and device is None:
            return DeviceUnknownError(details={"hint": "The device is no longer registered."})
        return None

    # ------------------------------------------------------------------- logout
    async def logout(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        device_id: uuid.UUID | None,
        access_jti: str,
        access_expires_in_seconds: int,
        all_devices: bool,
        actor: ActorContext,
    ) -> LogoutResult:
        """End the current session, or every session of the account."""
        async with self._database.transaction() as session:
            session_repo = SessionRepository(session)
            audit = AuditService(session)
            now = self._now()

            if all_devices:
                revoked = await session_repo.revoke_all_for_user(
                    user_id, reason=REASON_LOGOUT_ALL, when=now
                )
                scope = "all_devices"
            else:
                revoked = await session_repo.revoke_family(
                    session_id, reason=REASON_LOGOUT, when=now
                )
                scope = "current"

            audit.record(
                action=AuditAction.AUTH_LOGOUT,
                entity_type="user",
                entity_id=user_id,
                new_data={
                    "scope": scope,
                    "session_id": str(session_id),
                    "device_id": str(device_id) if device_id else None,
                    "revoked_tokens": revoked,
                },
                actor=actor,
            )

        # Denylist the access token as well. The family revocation above is what makes it
        # unusable immediately; this is the independent second switch from SECURITY.md §2.
        await self._revocation.revoke(access_jti, expires_in_seconds=access_expires_in_seconds)
        return LogoutResult(scope=scope, revoked_sessions=max(revoked, 1), session_id=session_id)

    # ----------------------------------------------------------------- sessions
    async def list_sessions(
        self, *, user_id: uuid.UUID, current_session_id: uuid.UUID
    ) -> list[tuple[SessionSummary, bool]]:
        """Active sessions of the caller, each flagged as current or not."""
        async with self._database.session() as session:
            summaries = await SessionRepository(session).list_active_sessions(
                user_id, now=self._now()
            )
        return [(summary, summary.family_id == current_session_id) for summary in summaries]

    async def revoke_session(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        current_session_id: uuid.UUID,
        actor: ActorContext,
    ) -> int:
        """Revoke one of the caller's own sessions.

        A session id belonging to somebody else is reported as *not found* rather than
        forbidden: the API must not confirm that another account's session exists.
        """
        async with self._database.transaction() as session:
            session_repo = SessionRepository(session)
            head = await session_repo.get_family_head(session_id)
            if head is None or head.user_id != user_id:
                raise ResourceNotFoundError("No such session for this account.")
            now = self._now()
            revoked = await session_repo.revoke_family(
                session_id, reason=REASON_SESSION_REVOKED, when=now
            )
            AuditService(session).record(
                action=AuditAction.SECURITY_SESSION_REVOKED,
                entity_type="refresh_token",
                entity_id=head.id,
                new_data={
                    "session_id": str(session_id),
                    "device_id": str(head.device_id) if head.device_id else None,
                    "was_current": session_id == current_session_id,
                },
                actor=actor,
            )
        return revoked

    # ----------------------------------------------------------------- password
    async def change_password(
        self,
        *,
        user_id: uuid.UUID,
        current_password: str,
        new_password: str,
        session_id: uuid.UUID,
        actor: ActorContext,
    ) -> PasswordChangeResult:
        """Change the caller's password and end every other session.

        "A password change ends the other sessions" is what makes a leaked credential
        recoverable: afterwards a stolen session can neither refresh nor keep using its
        access token, because the family is revoked and re-read on every request.
        """
        failure: NexusError | None = None
        result: PasswordChangeResult | None = None

        async with self._database.transaction() as session:
            users = UserRepository(session)
            session_repo = SessionRepository(session)
            audit = AuditService(session)
            now = self._now()

            user = await users.get_by_id(user_id, for_update=True)
            if user is None:  # pragma: no cover - the principal was loaded from this database
                failure = TokenInvalidError("The account no longer exists.")
            elif not self._hasher.verify(current_password, user.password_hash).is_valid:
                audit.record(
                    action=AuditAction.AUTH_PASSWORD_CHANGE_FAILED,
                    entity_type="user",
                    entity_id=user.id,
                    new_data={"reason": "INVALID_CURRENT_PASSWORD"},
                    actor=actor,
                )
                failure = InvalidCredentialsError("The current password is incorrect.")
            elif current_password == new_password:
                failure = ValidationError(
                    "The new password must be different from the current one.",
                    details={"fields": [{"field": "new_password", "code": "unchanged"}]},
                )
            else:
                try:
                    enforce_password_policy(
                        new_password, username=user.username, full_name=user.full_name
                    )
                except PasswordPolicyError as exc:
                    failure = ValidationError(
                        str(exc),
                        details={"fields": [{"field": "new_password", "code": "policy"}]},
                    )
                else:
                    user.password_hash = self._hasher.hash(new_password)
                    user.password_changed_at = now
                    user.must_change_password = False
                    revoked = await session_repo.revoke_all_for_user(
                        user.id,
                        reason=REASON_PASSWORD_CHANGED,
                        when=now,
                        except_family_id=session_id,
                    )
                    audit.record(
                        action=AuditAction.AUTH_PASSWORD_CHANGED,
                        entity_type="user",
                        entity_id=user.id,
                        new_data={
                            "revoked_sessions": revoked,
                            "argon2id": self._hasher.parameters,
                            "current_session_kept": str(session_id),
                        },
                        actor=actor,
                    )
                    result = PasswordChangeResult(password_changed_at=now, revoked_sessions=revoked)

        if failure is not None:
            raise failure
        if result is None:  # pragma: no cover - defensive
            raise AuthenticationError("The password change did not complete.")
        return result


def build_auth_service(
    *,
    database: Database,
    settings: Settings,
    tokens: TokenService,
    revocation_list: RevocationList,
) -> AuthService:
    """Construct the service with an Argon2id hasher built from validated settings."""
    hasher = build_password_hasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost,
        parallelism=settings.argon2_parallelism,
    )
    return AuthService(
        database=database,
        settings=settings,
        tokens=tokens,
        hasher=hasher,
        revocation_list=revocation_list,
    )


__all__ = [
    "AuthService",
    "AuthenticatedSession",
    "LoginRequestData",
    "LogoutResult",
    "PasswordChangeResult",
    "build_auth_service",
]
