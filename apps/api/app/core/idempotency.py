"""Idempotency-Key store and replay semantics (PART 40).

An ``Idempotency-Key`` is the difference between "the request was retried" and "the
customer paid twice". The frozen ``idempotency_keys`` table (Phase 0) stores one row per
``(user, endpoint, key)``; this module gives it the two behaviours a money-moving
endpoint needs:

* **Replay.** A key that already completed returns the *recorded* answer, byte for byte,
  without touching the ledger again.
* **Refusal.** The same key with a different request body is rejected
  (``IDEMPOTENCY_KEY_REUSED``): it is not a retry of anything, and answering it with the
  old response would silently discard the new request.

Design decisions that matter for financial safety:

* **The claim lives inside the caller's transaction.** A claim and the posting it
  describes commit together (PART 20). A crashed or rolled-back attempt leaves *no* row,
  so the operator's retry is a first attempt — the dangerous direction (a key that stays
  ``IN_PROGRESS`` forever and blocks a legitimate retry) cannot happen.
* **Concurrency is resolved by the unique index, not by application locking.** Two
  simultaneous duplicate requests: one inserts the claim, the other blocks on
  ``ux_idempotency_keys_scope`` until the first commits, then reads the completed row
  and replays it. The request that lost the race receives the answer its partner
  committed — never a second posting.
* **Money never enters the stored body as a JSON number.** Bodies are canonicalised to
  strings/ints before being written to ``response_body`` (JSONB), so a replayed amount is
  the same decimal string the original caller received — a JSON float would have lost
  precision before any validation could see it (PART 62).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    IdempotencyInProgressError,
    IdempotencyKeyReusedError,
    ValidationError,
)
from app.models.security import IdempotencyKey
from app.repositories.idempotency import IdempotencyRepository

# Statuses stored in ``idempotency_keys.status`` (the frozen CHECK constraint).
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"

# Endpoint names are part of the key scope, so they are named once instead of being typed
# at every call site: a typo would silently create a second namespace in which the same key
# posts a second time.
#
# The ledger's own default names the *service operation*, not an HTTP route, because the
# ledger has no public posting route: money moves through the service that owns the
# business document (Phase 5+), and that service passes its own endpoint name so a client
# retrying ``POST /api/v1/exchange`` replays the exchange posting, not a different one.
ENDPOINT_LEDGER_POSTING = "ledger:post"

# The exchange document's own endpoints. A key is scoped to (user, endpoint, key), so a
# client that retries ``POST /exchange`` replays the exchange — and a key it also used on
# ``/exchange/{id}/cancel`` can never be answered with the other operation's result.
ENDPOINT_EXCHANGE_CREATE = "exchange:create"
ENDPOINT_EXCHANGE_CANCEL = "exchange:cancel"
ENDPOINT_EXCHANGE_REVERSE = "exchange:reverse"

# The cash endpoints (Phase 6). ``cash:open`` and ``cash:close`` are separate keys from
# ``cash:in``/``cash:out`` for the same reason the exchange lifecycle moves are: a client
# that reuses one key across operations must be answered per operation, never with another
# operation's recorded result.
ENDPOINT_CASH_OPEN = "cash:open"
ENDPOINT_CASH_IN = "cash:in"
ENDPOINT_CASH_OUT = "cash:out"
ENDPOINT_CASH_ADJUSTMENT = "cash:adjustment"
ENDPOINT_CASH_CLOSE = "cash:close"
ENDPOINT_CASH_REVERSE = "cash:reverse"


def json_safe(value: Any) -> Any:
    """Return ``value`` as something :func:`json.dumps` serialises exactly.

    ``Decimal`` becomes a fixed-point **string** (never a float), ``UUID`` and
    ``datetime`` become strings, mappings and sequences are converted recursively.
    A ``float`` is refused outright: the only way one could appear here is a bug that
    would already have corrupted a money value.
    """
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        raise ValidationError(
            "A float cannot be part of an idempotent request body.",
            details={"value": repr(value), "reason": "MONEY_MUST_BE_DECIMAL"},
        )
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dt.datetime):
        return _wire_moment(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, Iterable):
        return [json_safe(item) for item in value]
    raise ValidationError(
        "Unsupported value in an idempotent request body.",
        details={"type": type(value).__name__},
    )


def _wire_moment(value: dt.datetime) -> str:
    """A moment as the API serialises it: RFC 3339, ``Z`` for UTC.

    The stored answer has to be the answer the caller received. A record that says
    ``+00:00`` where the wire said ``Z`` is the same instant but not the same document, and
    an auditor comparing a printed receipt with the idempotency row should not have to
    normalise the two by hand — nor should a replay, which is served from this JSON, return
    a differently spelled body than the original call did.
    """
    if value.tzinfo is not None and value.utcoffset() == dt.timedelta(0):
        return f"{value.astimezone(dt.UTC).replace(tzinfo=None).isoformat()}Z"
    return value.isoformat()


def canonical_request_hash(payload: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical form of ``payload`` (sorted keys, no whitespace).

    The hash answers one question: "is this the same request as the one that owns this
    key?". Sorting keys makes it independent of field order; canonicalising values makes
    ``Decimal('70')`` and ``Decimal('70.0000000000')`` hash differently — on purpose,
    because a different amount *is* a different request.
    """
    canonical = json.dumps(
        json_safe(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class IdempotencyReplay:
    """The answer a previous call with this key already produced."""

    status_code: int
    body: dict[str, Any]
    resource_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class IdempotencyRequest:
    """The identifying fields of one idempotent request."""

    key: uuid.UUID
    user_id: uuid.UUID
    endpoint: str
    request_hash: str
    device_id: uuid.UUID | None = None


class IdempotencyGuard:
    """Claims one key inside the caller's transaction and records its answer."""

    def __init__(self, session: AsyncSession, request: IdempotencyRequest) -> None:
        self._session = session
        self._repository = IdempotencyRepository(session)
        self._request = request
        self._row: IdempotencyKey | None = None

    async def claim(self) -> IdempotencyReplay | None:
        """Take ownership of the key, or return the answer it already holds.

        ``None`` means "you own this key" — do the work and call :meth:`complete`.
        """
        existing = await self._repository.get_for_update(self._request)
        if existing is not None:
            return self._handle_existing(existing)

        row = IdempotencyKey(
            key=self._request.key,
            user_id=self._request.user_id,
            device_id=self._request.device_id,
            endpoint=self._request.endpoint,
            request_hash=self._request.request_hash,
            status=STATUS_IN_PROGRESS,
        )
        try:
            # The savepoint is what makes the race survivable: a unique violation aborts
            # the *transaction* in PostgreSQL, so without it every later statement in
            # this transaction would fail with "current transaction is aborted".
            async with self._savepoint():
                self._repository.add(row)
                await self._repository.flush()
        except IntegrityError:
            raced = await self._repository.get_for_update(self._request)
            if raced is None:  # pragma: no cover - defensive: the row must be there now
                raise
            return self._handle_existing(raced)

        self._row = row
        return None

    def complete(
        self,
        *,
        status_code: int,
        body: Mapping[str, Any],
        resource_type: str,
        resource_id: uuid.UUID | None,
    ) -> None:
        """Record the answer this key produced (before the transaction commits)."""
        if self._row is None:  # pragma: no cover - defensive: complete follows claim
            raise RuntimeError("complete() was called without a claimed key")
        self._repository.mark_completed(
            self._row,
            status_code=status_code,
            body=json_safe(body),
            resource_type=resource_type,
            resource_id=resource_id,
        )

    @asynccontextmanager
    async def _savepoint(self) -> AsyncIterator[None]:
        async with self._session.begin_nested():
            yield

    def _handle_existing(self, row: IdempotencyKey) -> IdempotencyReplay | None:
        """Decide what an existing row means for this request."""
        request = self._request
        if row.status == STATUS_COMPLETED:
            if row.request_hash != request.request_hash:
                raise self._reused(row)
            return IdempotencyReplay(
                status_code=int(row.response_status or 200),
                body=dict(row.response_body or {}),
                resource_id=row.resource_id,
            )
        if row.status == STATUS_FAILED:
            if row.request_hash != request.request_hash:
                raise self._reused(row)
            # The earlier attempt recorded a failure. Nothing was committed, so the
            # operator may retry the same request — the row is reused, never duplicated.
            self._repository.reclaim(row)
            self._row = row
            return None
        # IN_PROGRESS inside a *committed* transaction means another attempt still holds
        # the key. Refuse instead of posting a second time: the safe direction for money.
        raise IdempotencyInProgressError(
            "This Idempotency-Key is already being processed.",
            details={
                "endpoint": request.endpoint,
                "idempotency_key": str(request.key),
                "hint": "Retry with the same key shortly; the first attempt is still running.",
            },
        )

    def _reused(self, row: IdempotencyKey) -> IdempotencyKeyReusedError:
        return IdempotencyKeyReusedError(
            "This Idempotency-Key was already used with a different request.",
            details={
                "endpoint": self._request.endpoint,
                "idempotency_key": str(self._request.key),
                "recorded_at": row.created_at.isoformat() if row.created_at else None,
            },
        )
