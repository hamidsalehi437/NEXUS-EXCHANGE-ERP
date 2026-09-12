"""Stored answers for ``Idempotency-Key`` (PART 40) — data access only.

One row per ``(user, endpoint, key)``, exactly as ``ux_idempotency_keys_scope`` declares
it. The semantics live in :mod:`app.core.idempotency`; this module only reads and writes
the row, so there is one statement per question and no business rule hiding in SQL.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.security import IdempotencyKey

# Mirrors the frozen ``idempotency_keys.status`` vocabulary (the CHECK constraint is the
# authority). It is spelled here rather than imported from ``app.core.idempotency``
# because the guard imports *this* module: a repository that imports the semantics it
# serves would make the two modules mutually dependent.
_STATUS_IN_PROGRESS = "IN_PROGRESS"


class IdempotencyScope(Protocol):
    """What the repository needs from a request: who, where, and which key.

    Structural rather than nominal so ``app.core.idempotency`` owns the request type
    without the data layer importing the semantic layer. The members are read-only
    properties because the request itself is a frozen value: the repository may read a
    scope, never rewrite the caller's identity.
    """

    @property
    def key(self) -> uuid.UUID: ...

    @property
    def user_id(self) -> uuid.UUID: ...

    @property
    def endpoint(self) -> str: ...


class IdempotencyRepository:
    """Reads and writes for ``idempotency_keys``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_update(self, request: IdempotencyScope) -> IdempotencyKey | None:
        """The row for this key, locked so two attempts cannot both decide to proceed.

        ``FOR UPDATE`` is what serialises two duplicates that arrive close together: the
        second waits, then sees the first one's committed state instead of inserting a
        second claim of its own.
        """
        statement = (
            select(IdempotencyKey)
            .where(
                IdempotencyKey.user_id == request.user_id,
                IdempotencyKey.endpoint == request.endpoint,
                IdempotencyKey.key == request.key,
            )
            .with_for_update()
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    def add(self, row: IdempotencyKey) -> None:
        self._session.add(row)

    async def flush(self) -> None:
        await self._session.flush()

    def mark_completed(
        self,
        row: IdempotencyKey,
        *,
        status_code: int,
        body: Mapping[str, Any],
        resource_type: str,
        resource_id: uuid.UUID | None,
    ) -> None:
        """Store the answer, so a retry replays it instead of repeating the posting."""
        row.status = "COMPLETED"
        row.response_status = status_code
        row.response_body = dict(body)
        row.resource_type = resource_type
        row.resource_id = resource_id
        row.completed_at = dt.datetime.now(tz=dt.UTC)

    def reclaim(self, row: IdempotencyKey) -> None:
        """Reuse a ``FAILED`` row for a retry of the same request."""
        row.status = _STATUS_IN_PROGRESS
        row.response_status = None
        row.response_body = None
        row.completed_at = None
