"""Refresh-token family queries — rotation, reuse detection and revocation (PART 24/42).

One *family* (``family_id``) is one session. Every refresh consumes the presented
token and issues its successor in the same family, so the database keeps the whole
chain: ``parent_id`` points at the token that was exchanged, ``replaced_by_id`` at the
token that took its place, and ``used_at``/``revoked_at`` record when it stopped being
valid. Presenting an already-exchanged token again is not a retry, it is evidence of
token theft, and the caller (:mod:`app.services.auth_service`) revokes the family.

All lookups that lead to a state change take ``SELECT … FOR UPDATE`` on the token row,
so two concurrent refreshes cannot both "win": the loser sees the row already
exchanged and is treated as reuse.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.branch import Branch
from app.models.device import Device
from app.models.security import RefreshToken
from app.models.user import User

# Revocation reasons stored in ``refresh_tokens.revoked_reason`` (VARCHAR(100)).
REASON_ROTATED = "ROTATED"
REASON_LOGOUT = "LOGOUT"
REASON_LOGOUT_ALL = "LOGOUT_ALL_DEVICES"
REASON_PASSWORD_CHANGED = "PASSWORD_CHANGED"  # noqa: S105 - a reason label, not a credential
REASON_SESSION_REVOKED = "SESSION_REVOKED"
REASON_DEVICE_REVOKED = "DEVICE_REVOKED"
REASON_USER_DEACTIVATED = "USER_DEACTIVATED"
REASON_REUSE_DETECTED = "REUSE_DETECTED"
REASON_EXPIRED = "EXPIRED"

# Reasons that mean "this token was legitimately exchanged"; replaying one is theft.
REUSE_EVIDENCE_REASONS = frozenset({REASON_ROTATED})

# Reasons that mean "the session was deliberately ended"; replaying one is a plain 401.
DELIBERATE_REVOCATION_REASONS = frozenset(
    {
        REASON_LOGOUT,
        REASON_LOGOUT_ALL,
        REASON_PASSWORD_CHANGED,
        REASON_SESSION_REVOKED,
        REASON_DEVICE_REVOKED,
        REASON_USER_DEACTIVATED,
    }
)


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Everything the per-request authorisation check needs, from one query."""

    token: RefreshToken
    user: User
    device: Device | None
    branch: Branch | None

    @property
    def family_id(self) -> uuid.UUID:
        return self.token.family_id


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """One active session, as listed by ``GET /auth/sessions``."""

    family_id: uuid.UUID
    device_id: uuid.UUID | None
    device_name: str | None
    platform: str | None
    branch_id: uuid.UUID | None
    branch_code: str | None
    issued_at: dt.datetime
    expires_at: dt.datetime
    last_used_at: dt.datetime | None
    ip_address: str | None
    user_agent: str | None


class SessionRepository:
    """Reads and writes for ``refresh_tokens``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -------------------------------------------------------------------- create
    def add_token(self, token: RefreshToken) -> RefreshToken:
        self._session.add(token)
        return token

    async def create_family(
        self,
        *,
        user_id: uuid.UUID,
        device_id: uuid.UUID | None,
        token_hash: str,
        expires_at: dt.datetime,
        ip_address: str | None,
        user_agent: str | None,
        issued_at: dt.datetime,
        family_id: uuid.UUID | None = None,
        parent_id: uuid.UUID | None = None,
    ) -> RefreshToken:
        """Insert a refresh token, starting a new family unless one is given."""
        token = RefreshToken(
            id=uuid.uuid4(),
            user_id=user_id,
            device_id=device_id,
            family_id=family_id or uuid.uuid4(),
            parent_id=parent_id,
            token_hash=token_hash,
            issued_at=issued_at,
            expires_at=expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return self.add_token(token)

    # ------------------------------------------------------------------ lookups
    async def get_by_hash(
        self, token_hash: str, *, for_update: bool = False
    ) -> RefreshToken | None:
        statement = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_token(self, token_id: uuid.UUID) -> RefreshToken | None:
        return (
            await self._session.execute(select(RefreshToken).where(RefreshToken.id == token_id))
        ).scalar_one_or_none()

    async def get_family_head(self, family_id: uuid.UUID) -> RefreshToken | None:
        """Newest token in a family (the one a client should be holding)."""
        return (
            await self._session.execute(
                select(RefreshToken)
                .where(RefreshToken.family_id == family_id)
                .order_by(RefreshToken.issued_at.desc(), RefreshToken.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def load_session_context(self, family_id: uuid.UUID) -> SessionContext | None:
        """Token + user + device + branch for one session, in a single round trip.

        This is the query behind every authenticated request: it proves the session
        still exists, was not revoked, the account is active and the device is not
        revoked — before any permission is evaluated.
        """
        row = (
            await self._session.execute(
                select(RefreshToken, User, Device, Branch)
                .join(User, User.id == RefreshToken.user_id)
                .outerjoin(Device, Device.id == RefreshToken.device_id)
                .outerjoin(Branch, Branch.id == Device.branch_id)
                .where(RefreshToken.family_id == family_id)
                .order_by(RefreshToken.issued_at.desc(), RefreshToken.id.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        token, user, device, branch = row
        return SessionContext(token=token, user=user, device=device, branch=branch)

    async def list_active_sessions(
        self, user_id: uuid.UUID, *, now: dt.datetime
    ) -> list[SessionSummary]:
        """Active families for one user: newest token per family, not revoked, not expired."""
        heads = (
            select(
                RefreshToken.family_id.label("family_id"),
                func.max(RefreshToken.issued_at).label("issued_at"),
            )
            .where(RefreshToken.user_id == user_id)
            .group_by(RefreshToken.family_id)
            .subquery()
        )
        rows = (
            await self._session.execute(
                select(RefreshToken, Device, Branch)
                .join(
                    heads,
                    (RefreshToken.family_id == heads.c.family_id)
                    & (RefreshToken.issued_at == heads.c.issued_at),
                )
                .outerjoin(Device, Device.id == RefreshToken.device_id)
                .outerjoin(Branch, Branch.id == Device.branch_id)
                .where(
                    RefreshToken.user_id == user_id,
                    RefreshToken.revoked_at.is_(None),
                    RefreshToken.expires_at > now,
                )
                .order_by(RefreshToken.issued_at.desc())
            )
        ).all()

        summaries: list[SessionSummary] = []
        for token, device, branch in rows:
            last_used = await self._session.scalar(
                select(func.max(RefreshToken.used_at)).where(
                    RefreshToken.family_id == token.family_id
                )
            )
            summaries.append(
                SessionSummary(
                    family_id=token.family_id,
                    device_id=device.id if device else None,
                    device_name=device.device_name if device else None,
                    platform=device.platform if device else None,
                    branch_id=branch.id if branch else None,
                    branch_code=branch.code if branch else None,
                    issued_at=token.issued_at,
                    expires_at=token.expires_at,
                    last_used_at=last_used,
                    ip_address=str(token.ip_address) if token.ip_address else None,
                    user_agent=token.user_agent,
                )
            )
        return summaries

    # -------------------------------------------------------------------- mutate
    async def mark_rotated(
        self, token: RefreshToken, *, successor_id: uuid.UUID, used_at: dt.datetime
    ) -> None:
        """Consume a token: mark it used, revoked and linked to its successor."""
        token.used_at = used_at
        token.revoked_at = used_at
        token.revoked_reason = REASON_ROTATED
        token.replaced_by_id = successor_id

    async def revoke_token(self, token: RefreshToken, *, reason: str, when: dt.datetime) -> None:
        if token.revoked_at is None:
            token.revoked_at = when
            token.revoked_reason = reason

    async def revoke_token_by_id(
        self, token_id: uuid.UUID, *, reason: str, when: dt.datetime
    ) -> int:
        result = await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.id == token_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=when, revoked_reason=reason)
        )
        return int(cast(CursorResult[object], result).rowcount or 0)

    async def revoke_family(self, family_id: uuid.UUID, *, reason: str, when: dt.datetime) -> int:
        """Revoke every live token of a family; returns how many rows changed."""
        result = await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=when, revoked_reason=reason)
        )
        return int(cast(CursorResult[object], result).rowcount or 0)

    async def revoke_all_for_user(
        self,
        user_id: uuid.UUID,
        *,
        reason: str,
        when: dt.datetime,
        except_family_id: uuid.UUID | None = None,
    ) -> int:
        """Revoke every live session of a user, optionally sparing the current one."""
        statement = update(RefreshToken).where(
            RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
        )
        if except_family_id is not None:
            statement = statement.where(RefreshToken.family_id != except_family_id)
        result = await self._session.execute(
            statement.values(revoked_at=when, revoked_reason=reason)
        )
        return int(cast(CursorResult[object], result).rowcount or 0)

    async def revoke_all_for_device(
        self, device_id: uuid.UUID, *, reason: str, when: dt.datetime
    ) -> int:
        """Revoke every live session bound to a device (device revocation, PART 42)."""
        result = await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.device_id == device_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=when, revoked_reason=reason)
        )
        return int(cast(CursorResult[object], result).rowcount or 0)

    async def count_family_tokens(self, family_id: uuid.UUID) -> int:
        """How many tokens a family has issued, i.e. how many rotations happened."""
        total = await self._session.scalar(
            select(func.count())
            .select_from(RefreshToken)
            .where(RefreshToken.family_id == family_id)
        )
        return int(total or 0)

    async def count_live_sessions(self, user_id: uuid.UUID, *, now: dt.datetime) -> int:
        families = await self._session.scalar(
            select(func.count(func.distinct(RefreshToken.family_id))).where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
        )
        return int(families or 0)

    async def active_families(self, user_id: uuid.UUID) -> Sequence[uuid.UUID]:
        rows = await self._session.execute(
            select(RefreshToken.family_id)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .distinct()
        )
        return [row[0] for row in rows.all()]
