"""Audit-trail queries (PART 18).

``audit_logs`` is append-only: there is no update and no delete here, and the
repository deliberately exposes no method that could look like one. Rows are inserted
through :class:`~app.repositories.audit.AuditRepository.record`, which also performs a
defensive scrub of anything that looks like credential material, so a caller bug
cannot put a password or a token into the permanent evidence trail.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog

# Keys that must never be stored in an audit row, matched as substrings.
_FORBIDDEN_KEY_PARTS = ("password", "passwd", "secret", "token", "authorization", "hash")
_SCRUBBED = "[redacted]"


def scrub_payload(data: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Drop credential-shaped keys from an audit payload.

    The audit trail is the evidence an auditor reads; it records *what* happened, never
    the material that would let the reader replay it. Recursion covers nested
    structures (for example a device object inside a session payload).
    """
    if data is None:
        return None
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if any(part in key.lower() for part in _FORBIDDEN_KEY_PARTS):
            cleaned[key] = _SCRUBBED
        elif isinstance(value, Mapping):
            cleaned[key] = scrub_payload(value)
        else:
            cleaned[key] = value
    return cleaned


class AuditRepository:
    """Reads and appends for ``audit_logs``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        device_id: uuid.UUID | None = None,
        old_data: Mapping[str, Any] | None = None,
        new_data: Mapping[str, Any] | None = None,
        ip_address: str | None = None,
        request_id: str | None = None,
    ) -> AuditLog:
        """Insert one audit row (``seq``/``prev_hash``/``chain_hash`` are DB-generated)."""
        row = AuditLog(
            user_id=user_id,
            device_id=device_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            old_data=scrub_payload(old_data),
            new_data=scrub_payload(new_data),
            ip_address=ip_address,
            request_id=request_id,
        )
        self._session.add(row)
        return row

    async def list_entries(
        self,
        *,
        action: str | None = None,
        user_id: uuid.UUID | None = None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        since: dt.datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[AuditLog], int]:
        statement = select(AuditLog).order_by(AuditLog.seq.desc())
        if action:
            statement = statement.where(AuditLog.action == action)
        if user_id is not None:
            statement = statement.where(AuditLog.user_id == user_id)
        if entity_type:
            statement = statement.where(AuditLog.entity_type == entity_type)
        if entity_id is not None:
            statement = statement.where(AuditLog.entity_id == entity_id)
        if since is not None:
            statement = statement.where(AuditLog.created_at >= since)
        total = await self._session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = await self._session.execute(statement.limit(limit).offset(offset))
        return list(rows.scalars().all()), int(total or 0)

    async def count_actions(self, action: str, *, since: dt.datetime | None = None) -> int:
        statement = select(func.count()).select_from(AuditLog).where(AuditLog.action == action)
        if since is not None:
            statement = statement.where(AuditLog.created_at >= since)
        return int(await self._session.scalar(statement) or 0)

    async def chain_is_valid(self) -> bool:
        """Verify the hash chain with the database's own function (PART 18).

        ``verify_audit_chain()`` returns the number of broken links; anything but zero
        means the evidence trail was tampered with and must be investigated.
        """
        broken = await self._session.scalar(select(func.verify_audit_chain()))
        return int(broken or 0) == 0
