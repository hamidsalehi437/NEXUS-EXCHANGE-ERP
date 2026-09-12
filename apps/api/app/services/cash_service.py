"""Cash management: shifts, cash in/out, adjustments, reconciliation (Phase 6).

``API_CONTRACT.md`` §9.4 in one place. The service owns the *control* of a till — who may
open it, what may happen inside it, and how a physical count is reconciled against what the
books say — and it owns none of the *accounting*. Every journal entry a cash act produces is
posted by :class:`app.services.accounting_service.AccountingService` (PART 46): the ledger
remains the single authority for what the books say, and ``cash_movements`` remains the
physical evidence beside it. There is no second ledger here, no cached balance, and no code
path that writes a journal line.

The rules this module implements, in the order the contract states them:

1. **A shift is an open period with one owner.** Opening locks the branch row, so the "is a
   shift already open?" answer cannot be raced; a second open at the same branch is refused
   with the session that holds it (``CASH_SESSION_ALREADY_OPEN``). Every movement and every
   close locks the session row first, so cash can never land in a shift that is closing at
   that instant, and a closed shift can never be posted to (``CASH_SESSION_NOT_OPEN``).
2. **An opening count is a statement about the drawer, not an invitation to create money.**
   §6.1's opening entry (``Dr Cash / Cr 6000 Opening Offset``) is posted only for a drawer
   the books know nothing about *and* whose physical position is empty — that is what an
   opening balance is. Where the ledger already carries cash, the declared count must equal
   the carried position, and a disagreement is refused with both numbers
   (``CASH_OPENING_MISMATCH``) instead of being written to income or equity. Nothing about
   the opening can be edited afterwards: the movements are immutable and the session line is
   written once.
3. **Expected is derived, never asserted.** A shift's expected amount per currency is
   ``opening_declared + the signed movements of that shift`` *excluding* the ``OPENING``
   movement, because the declared count already includes the cash that movement introduced
   (§6.1 posts the count, it does not add to it) — and the drawer's physical position at that
   moment is checked to equal that number. The count is written beside it and the database
   derives ``difference = counted - expected`` (§9.4, ``nexus_validate_cash_session_line``).
4. **A difference is money, so it is posted.** A non-zero difference is posted as an
   ``ADJUSTMENT`` against ``5090 Cash Short / Over`` (§6.4) — under ``cash.adjust``, the
   permission the contract reserves for corrections — and never by editing a count, a
   movement or a stored total.
5. **Corrections are compensating entries.** Reversing a movement posts the ledger's mirror
   entry and one opposite physical movement; the original stays exactly as posted, and a
   second reversal is refused (``ALREADY_REVERSED``). A movement that belongs to another
   document (an exchange deal, a shift opening) is refused as not reversible here: that
   document owns its own correction.
6. **Amounts are ``Decimal`` from the first parse to the last write.** Money is validated
   with the ledger's own rules (bounds, currency scale, positivity) *before* anything is
   posted, and every monetary field leaves as a fixed-point string.

Two deliberate strictnesses, both documented rather than assumed:

* **One open shift per branch.** The frozen schema enforces "one open session per device"
   with a partial unique index and leaves the device-less case to a branch lock. Because the
   physical till is per ``(branch, currency)`` — ``v_cash_position`` has no session in its
   grain — a second open shift at the same branch would share one drawer with the first and
   make each shift's expected amount wrong. Opening therefore takes the branch row lock and
   refuses a second open shift at the branch, whatever device asks for it.
* **Declaring a brand-new drawer is an accounting act.** The opening entry is the ledger's
   ``OPENING_BALANCE`` document, whose posting authority is ``accounts.manage``
   (``POSTING_AUTHORITY``). A cashier may open and work a shift whose cash the books already
   carry; the first-ever declaration of a drawer needs an accountant, owner or administrator,
   and the ledger says so.

Offline hooks (PART 34, Phase 12/13 build the transport, not this): every mutation takes an
``Idempotency-Key`` through the existing store, an optional ``client_event_id`` makes a
movement replay-safe by its own name, movement references are fresh UUIDs (deterministic per
event, never derived from a clock), and the server owns every timestamp and every rate.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_actions import AuditAction
from app.core.config import Settings
from app.core.database import Database
from app.core.exceptions import (
    AlreadyReversedError,
    CashCounterAccountRequiredError,
    CashMovementNotReversibleError,
    CashOpeningMismatchError,
    CashReconciliationIncompleteError,
    CashSessionAlreadyOpenError,
    CashSessionNotOpenError,
    ConflictError,
    CurrencyInactiveError,
    DataIntegrityError,
    InsufficientBalanceError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ValidationError,
)
from app.core.idempotency import (
    ENDPOINT_CASH_ADJUSTMENT,
    ENDPOINT_CASH_CLOSE,
    ENDPOINT_CASH_IN,
    ENDPOINT_CASH_OPEN,
    ENDPOINT_CASH_OUT,
    ENDPOINT_CASH_REVERSE,
    IdempotencyGuard,
    IdempotencyRequest,
    canonical_request_hash,
)
from app.core.money import (
    MONEY_SCALE,
    format_decimal,
    money_difference,
    quantize_money,
)
from app.core.permissions import Permission
from app.models.account import Account
from app.models.branch import Branch
from app.models.cash import CashSession, CashSessionLine
from app.models.currency import Currency
from app.models.device import Device
from app.repositories.cash import (
    REFERENCE_TYPE_CASH,
    REFERENCE_TYPE_OPENING,
    REFERENCE_TYPE_REVERSAL,
    REVERSIBLE_REFERENCE_TYPES,
    STATUS_CLOSED,
    STATUS_OPEN,
    CashMovementRepository,
    CashSessionRepository,
)
from app.repositories.devices import DeviceRepository
from app.repositories.ledger_master import AccountRepository
from app.repositories.masterdata import CurrencyRepository
from app.services.accounting_service import (
    CASH_ACCOUNT_CODE_PATTERN,
    AccountingService,
    CashMovementSpec,
    build_accounting_service,
    require_positive_money,
    validate_money,
)
from app.services.audit_service import ActorContext, AuditService

# The two chart accounts the accounting model names for cash control (§5, §6.1, §6.4).
# Resolved by code because the model names them; a chart without them is a misconfigured
# installation and the refusal says which code is missing.
OPENING_OFFSET_ACCOUNT_CODE = "6000"
SHORT_OVER_ACCOUNT_CODE = "5090"

# The movement types this module can record directly, and the mirror of each. ``OPENING``
# belongs to the session lifecycle and ``CLOSING`` is a reconciliation snapshot (§6.4), so
# neither is recordable as a standalone movement.
DIRECT_MOVEMENT_TYPES = ("IN", "OUT", "ADJUSTMENT")
MIRROR_MOVEMENT: dict[str, str] = {
    "IN": "OUT",
    "OUT": "IN",
    "OPENING": "OUT",
    "EXPENSE": "IN",
    "ADJUSTMENT": "ADJUSTMENT",
}

# What the audit trail calls each operation, and the permission the contract requires for it
# (§8). One table: an endpoint cannot invent a permission by accident.
OPERATION_PERMISSIONS: dict[str, Permission] = {
    "open": Permission.CASH_CREATE,
    "in": Permission.CASH_CREATE,
    "out": Permission.CASH_CREATE,
    "adjustment": Permission.CASH_ADJUST,
    "close": Permission.CASH_CLOSE,
    "reverse": Permission.CASH_ADJUST,
    "view": Permission.CASH_VIEW,
}


@dataclass(frozen=True, slots=True)
class CashOpening:
    """One currency's opening count at shift start."""

    currency_id: uuid.UUID
    amount: Decimal
    exchange_rate: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OpenSessionRequest:
    """A request to open a shift (§9.4 ``POST /cash/open``)."""

    branch_id: uuid.UUID
    device_id: uuid.UUID | None = None
    openings: Sequence[CashOpening] = ()
    notes: str | None = None

    def fingerprint(self) -> dict[str, Any]:
        """The economic identity of the request, for idempotency and offline replay.

        The declared counts and the drawer they belong to — not the device ident, which is
        transport, and not the notes, which are a comment. Deterministically ordered, so a
        client that sends the same balances in another order is asking the same question.
        """
        return {
            "branch_id": self.branch_id,
            "openings": [
                {
                    "currency_id": opening.currency_id,
                    "amount": _fingerprint_money(opening.amount),
                    "exchange_rate": _fingerprint_money(opening.exchange_rate),
                }
                for opening in sorted(self.openings, key=lambda item: str(item.currency_id))
            ],
        }


@dataclass(frozen=True, slots=True)
class CashMovementRequest:
    """A movement request (§9.4 ``/cash/in``, ``/cash/out``, ``/cash/adjustment``)."""

    branch_id: uuid.UUID
    currency_id: uuid.UUID
    amount: Decimal
    session_id: uuid.UUID | None = None
    device_id: uuid.UUID | None = None
    counter_account_id: uuid.UUID | None = None
    adjustment_sign: int | None = None
    reason: str | None = None
    description: str | None = None
    client_event_id: uuid.UUID | None = None
    transaction_date: dt.datetime | None = None

    def fingerprint(self, *, movement_type: str) -> dict[str, Any]:
        """The economic identity of the movement (see :meth:`OpenSessionRequest.fingerprint`)."""
        return {
            "movement_type": movement_type,
            "branch_id": self.branch_id,
            "session_id": self.session_id,
            "currency_id": self.currency_id,
            "amount": _fingerprint_money(self.amount),
            "counter_account_id": self.counter_account_id,
            "adjustment_sign": self.adjustment_sign,
            "reason": self.reason,
            "client_event_id": self.client_event_id,
        }


@dataclass(frozen=True, slots=True)
class CashCount:
    """One currency's physical count at closing time."""

    currency_id: uuid.UUID
    amount: Decimal


@dataclass(frozen=True, slots=True)
class CloseSessionRequest:
    """A request to close a shift (§9.4 ``POST /cash/close``)."""

    session_id: uuid.UUID
    counted: Sequence[CashCount] = ()
    notes: str | None = None

    def fingerprint(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "counted": [
                {"currency_id": item.currency_id, "amount": _fingerprint_money(item.amount)}
                for item in sorted(self.counted, key=lambda item: str(item.currency_id))
            ],
        }


@dataclass(frozen=True, slots=True)
class CashResult:
    """A mutation's recorded answer (the same shape the API renders, replay included)."""

    payload: Mapping[str, Any]
    status_code: int
    replayed: bool = False
    session_id: uuid.UUID | None = None
    movement_id: uuid.UUID | None = None
    journal_entry_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class CashMovementView:
    """One physical cash movement, as the API prints it."""

    row: Mapping[str, Any]
    reversal_movement_id: uuid.UUID | None = None

    def to_payload(self) -> dict[str, Any]:
        row = self.row
        return {
            "id": str(row["id"]),
            "branch_id": str(row["branch_id"]),
            "branch_code": row["branch_code"],
            "account_id": str(row["account_id"]),
            "account_code": row["account_code"],
            "currency_id": str(row["currency_id"]),
            "currency_code": row["currency_code"],
            "movement_type": row["movement_type"],
            "amount": _money(row["amount"]),
            "signed_amount": _money(row["signed_amount"]),
            "adjustment_sign": row["adjustment_sign"],
            "reference_type": row["reference_type"],
            "reference_id": str(row["reference_id"]) if row["reference_id"] else None,
            "description": row["description"],
            "session_id": str(row["cash_session_id"]) if row["cash_session_id"] else None,
            "session_status": row["session_status"],
            "device_id": str(row["device_id"]) if row["device_id"] else None,
            "journal_entry_id": str(row["journal_entry_id"]) if row["journal_entry_id"] else None,
            "client_event_id": str(row["client_event_id"]) if row["client_event_id"] else None,
            "reversed_by_movement_id": (
                str(self.reversal_movement_id) if self.reversal_movement_id else None
            ),
            "created_by": str(row["created_by"]) if row["created_by"] else None,
            "created_by_username": row["created_by_username"],
            "created_at": _iso(row["created_at"]),
        }


@dataclass(frozen=True, slots=True)
class CashSessionView:
    """A shift with its reconciliation lines and (optionally) its movements."""

    row: Mapping[str, Any]
    lines: tuple[Mapping[str, Any], ...] = ()
    movements: tuple[CashMovementView, ...] = ()
    adjustment_entry_ids: Mapping[uuid.UUID, uuid.UUID] = field(default_factory=dict)

    @property
    def id(self) -> uuid.UUID:
        return uuid.UUID(str(self.row["id"]))

    @property
    def branch_id(self) -> uuid.UUID:
        return uuid.UUID(str(self.row["branch_id"]))

    @property
    def status(self) -> str:
        return str(self.row["status"])

    def to_payload(self) -> dict[str, Any]:
        row = self.row
        return {
            "id": str(row["id"]),
            "session_id": str(row["id"]),
            "branch_id": str(row["branch_id"]),
            "branch_code": row["branch_code"],
            "branch_name": row["branch_name"],
            "branch_timezone": row["branch_timezone"],
            "business_date": _business_date(row),
            "device_id": str(row["device_id"]) if row["device_id"] else None,
            "device_uuid": str(row["device_uuid"]) if row.get("device_uuid") else None,
            "device_name": row["device_name"],
            "status": row["status"],
            "opened_by": str(row["opened_by"]) if row["opened_by"] else None,
            "opened_by_username": row["opened_by_username"],
            "opened_at": _iso(row["opened_at"]),
            "closed_by": str(row["closed_by"]) if row["closed_by"] else None,
            "closed_by_username": row["closed_by_username"],
            "closed_at": _iso(row["closed_at"]),
            "notes": row["notes"],
            "lines": [self._line_payload(line) for line in self.lines],
            "movements": [movement.to_payload() for movement in self.movements],
            "movement_count": len(self.movements),
            "has_variance": any(
                line["difference"] is not None and Decimal(str(line["difference"])) != 0
                for line in self.lines
            ),
        }

    def _line_payload(self, line: Mapping[str, Any]) -> dict[str, Any]:
        entry_id = self.adjustment_entry_ids.get(uuid.UUID(str(line["currency_id"])))
        return {
            "id": str(line["id"]),
            "currency_id": str(line["currency_id"]),
            "currency_code": line["currency_code"],
            "currency_name": line["currency_name"],
            "currency_decimal_places": line["currency_decimal_places"],
            "opening_declared": _money(line["opening_declared"]),
            "expected_amount": _optional_money(line["expected_amount"]),
            "counted_amount": _optional_money(line["counted_amount"]),
            "difference": _optional_money(line["difference"]),
            "adjustment_journal_entry_id": str(entry_id) if entry_id else None,
        }


@dataclass(frozen=True, slots=True)
class CashBalanceRow:
    """One branch/currency position: what the drawers hold and what the books carry."""

    branch_id: uuid.UUID
    branch_code: str
    currency_id: uuid.UUID
    currency_code: str
    physical_balance: Decimal
    ledger_functional_balance: Decimal
    ledger_quantity: Decimal

    @property
    def reconciled(self) -> bool:
        return self.physical_balance == self.ledger_quantity

    def to_payload(self) -> dict[str, Any]:
        return {
            "branch_id": str(self.branch_id),
            "branch_code": self.branch_code,
            "currency_id": str(self.currency_id),
            "currency_code": self.currency_code,
            "physical_balance": format_decimal(self.physical_balance),
            "ledger_functional_balance": format_decimal(self.ledger_functional_balance),
            "ledger_quantity": format_decimal(self.ledger_quantity),
            "reconciled": self.reconciled,
            "source": "cash_movements",
        }


class CashService:
    """The cash module's service (PART 63: routes own no logic)."""

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        accounting: AccountingService | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._accounting = accounting or build_accounting_service(
            database=database, settings=settings
        )

    @property
    def accounting(self) -> AccountingService:
        return self._accounting

    # ==================================================================== opening
    async def open_session(
        self,
        *,
        actor: ActorContext,
        request: OpenSessionRequest,
        idempotency_key: uuid.UUID | None = None,
    ) -> CashResult:
        """Open a shift: its opening counts, and the opening entries they justify (§6.1)."""
        await self._authorize(actor, "open", context={"branch_id": str(request.branch_id)})
        await self._accounting.assert_branch_scope(
            actor, branch_id=request.branch_id, context={"operation": "cash.open"}
        )
        openings = self._validate_openings(request.openings)

        async with self._accounting.document_transaction() as session:
            sessions = CashSessionRepository(session)
            # The branch row is the mutex for "does this branch already have a shift open?":
            # the partial unique index on ``device_id`` covers device-bound sessions, and the
            # device-less and cross-device cases are decided here, under one lock.
            await self._require_branch(await sessions.lock_branch(request.branch_id))
            device_id = await self._resolve_device(
                session, actor=actor, device_id=request.device_id, branch_id=request.branch_id
            )
            existing = await self._open_session_at(session, branch_id=request.branch_id)
            if existing is not None:
                raise CashSessionAlreadyOpenError(
                    "This branch already has an open cash session.",
                    details={
                        "branch_id": str(request.branch_id),
                        "session_id": str(existing.id),
                        "opened_by": str(existing.opened_by),
                        "opened_at": existing.opened_at.isoformat(),
                        "hint": "Close the open shift before opening another one.",
                    },
                )

            guard = await self._claim(
                session,
                actor=actor,
                key=idempotency_key,
                endpoint=ENDPOINT_CASH_OPEN,
                fingerprint=request.fingerprint(),
            )
            if isinstance(guard, CashResult):
                return guard

            cash_session = CashSession(
                branch_id=request.branch_id,
                device_id=device_id,
                opened_by=actor.user_id,
                status=STATUS_OPEN,
                notes=request.notes,
            )
            sessions.add(cash_session)
            await sessions.flush()

            for opening in openings:
                currency = await self._require_currency(
                    session, opening.currency_id, "openings[].currency_id"
                )
                amount = self._check_money(
                    opening.amount, currency=currency, field="openings[].amount"
                )
                drawer = await self._accounting.inventory_account(
                    session, branch_id=request.branch_id, currency_id=currency.id
                )
                carried = await self._carried_position(
                    session, branch_id=request.branch_id, currency_id=currency.id
                )
                if carried.ledger_quantity != carried.physical_balance:
                    raise DataIntegrityError(
                        "The ledger and the cash movements disagree about this drawer; "
                        "reconcile it before opening a shift on it.",
                        details={
                            "reason": "UNRECONCILED_POSITION",
                            "branch_id": str(request.branch_id),
                            "currency_code": currency.code,
                            "ledger_quantity": format_decimal(carried.ledger_quantity),
                            "physical_balance": format_decimal(carried.physical_balance),
                        },
                    )
                if carried.ledger_quantity == 0:
                    # §6.1: the drawer is not on the books yet, so the count *is* the opening
                    # balance and the entry says where it came from (6000 Opening Offset).
                    if amount > 0:
                        await self._post_opening(
                            session,
                            actor=actor,
                            branch_id=request.branch_id,
                            cash_session=cash_session,
                            drawer=drawer,
                            currency=currency,
                            amount=amount,
                            exchange_rate=opening.exchange_rate,
                            device_id=device_id,
                        )
                elif amount != carried.ledger_quantity:
                    # The books already carry cash in this drawer: re-posting the count would
                    # inflate both the position and the equity behind it, so the count has to
                    # agree with the books instead (§6.1 has no "top-up" entry).
                    raise CashOpeningMismatchError(
                        "The counted opening balance differs from the cash the books carry.",
                        details={
                            "branch_id": str(request.branch_id),
                            "currency_id": str(currency.id),
                            "currency_code": currency.code,
                            "declared_amount": format_decimal(amount),
                            "carried_amount": format_decimal(carried.ledger_quantity),
                            "hint": (
                                "Post the difference as a cash adjustment first, then open "
                                "the shift with the reconciled balance."
                            ),
                        },
                    )
                sessions.add_line(
                    CashSessionLine(
                        cash_session_id=cash_session.id,
                        currency_id=currency.id,
                        opening_declared=amount,
                    )
                )
            await sessions.flush()

            AuditService(session).record(
                action=AuditAction.CASH_SESSION_OPENED,
                entity_type="cash_session",
                entity_id=cash_session.id,
                new_data={
                    "branch_id": str(request.branch_id),
                    "device_id": str(device_id) if device_id else None,
                    "opening_lines": [
                        {
                            "currency_id": str(opening.currency_id),
                            "declared": format_decimal(opening.amount),
                        }
                        for opening in openings
                    ],
                    "notes": request.notes,
                },
                actor=actor,
            )
            payload = (
                await self._load_session(session, cash_session.id, with_movements=True)
            ).to_payload()
            self._complete(guard, status_code=201, payload=payload, resource_id=cash_session.id)

        return CashResult(
            payload=payload, status_code=201, session_id=cash_session.id, replayed=False
        )

    # ================================================================== movements
    async def record_in(
        self,
        *,
        actor: ActorContext,
        request: CashMovementRequest,
        idempotency_key: uuid.UUID,
    ) -> CashResult:
        """Record cash received into the drawer (``POST /cash/in``, §6.4)."""
        return await self._record_movement(
            actor=actor,
            request=request,
            movement_type="IN",
            operation="in",
            endpoint=ENDPOINT_CASH_IN,
            audit_action=AuditAction.CASH_MOVEMENT_RECORDED,
            idempotency_key=idempotency_key,
        )

    async def record_out(
        self,
        *,
        actor: ActorContext,
        request: CashMovementRequest,
        idempotency_key: uuid.UUID,
    ) -> CashResult:
        """Record cash paid out of the drawer (``POST /cash/out``, §6.4)."""
        return await self._record_movement(
            actor=actor,
            request=request,
            movement_type="OUT",
            operation="out",
            endpoint=ENDPOINT_CASH_OUT,
            audit_action=AuditAction.CASH_MOVEMENT_RECORDED,
            idempotency_key=idempotency_key,
        )

    async def record_adjustment(
        self,
        *,
        actor: ActorContext,
        request: CashMovementRequest,
        idempotency_key: uuid.UUID | None = None,
    ) -> CashResult:
        """Record a short/over correction against ``5090`` (``POST /cash/adjustment``, §6.4)."""
        if request.adjustment_sign not in (-1, 1):
            raise ValidationError(
                "An adjustment must state its direction.",
                details={
                    "fields": [{"field": "adjustment_sign", "code": "required"}],
                    "allowed": [-1, 1],
                },
            )
        if not request.reason or not request.reason.strip():
            raise ValidationError(
                "An adjustment needs a reason: the auditor reads it, not the amount.",
                details={"fields": [{"field": "reason", "code": "required"}]},
            )
        return await self._record_movement(
            actor=actor,
            request=request,
            movement_type="ADJUSTMENT",
            operation="adjustment",
            endpoint=ENDPOINT_CASH_ADJUSTMENT,
            audit_action=AuditAction.CASH_ADJUSTMENT_RECORDED,
            idempotency_key=idempotency_key,
        )

    async def _record_movement(
        self,
        *,
        actor: ActorContext,
        request: CashMovementRequest,
        movement_type: str,
        operation: str,
        endpoint: str,
        audit_action: AuditAction,
        idempotency_key: uuid.UUID | None,
    ) -> CashResult:
        """The one path every standalone cash movement takes (in, out, adjustment).

        Authorization, branch scope, the open session, the drawer, the amount — all decided
        before anything is written; then the journal entry (the ledger's job), the physical
        movement, the audit row, and the recorded answer, in one transaction (PART 20).
        """
        await self._authorize(
            actor,
            operation,
            context={
                "branch_id": str(request.branch_id),
                "currency_id": str(request.currency_id),
                "movement_type": movement_type,
            },
        )
        await self._accounting.assert_branch_scope(
            actor, branch_id=request.branch_id, context={"operation": f"cash.{operation}"}
        )

        async with self._accounting.document_transaction() as session:
            guard = await self._claim(
                session,
                actor=actor,
                key=idempotency_key,
                endpoint=endpoint,
                fingerprint=request.fingerprint(movement_type=movement_type),
            )
            if isinstance(guard, CashResult):
                return guard

            replayed = await self._existing_client_event(session, actor=actor, request=request)
            if replayed is not None:
                payload = (await self._movement_view(session, _uuid(replayed["id"]))).to_payload()
                self._complete(
                    guard, status_code=201, payload=payload, resource_id=_uuid(replayed["id"])
                )
                return CashResult(
                    payload=payload,
                    status_code=201,
                    replayed=True,
                    movement_id=_uuid(replayed["id"]),
                )

            cash_session = await self._require_open_session(session, actor=actor, request=request)
            currency = await self._require_currency(session, request.currency_id, "currency_id")
            amount = self._check_money(request.amount, currency=currency, field="amount")
            require_positive_money(amount, field="amount")
            if movement_type == "ADJUSTMENT":
                counter_account_id = await self._account_by_code(
                    session, code=SHORT_OVER_ACCOUNT_CODE, field="counter_account_id"
                )
            elif request.counter_account_id is None:
                raise CashCounterAccountRequiredError(
                    f"A cash {movement_type.lower()} needs the account it moves value from or to.",
                    details={
                        "fields": [
                            {
                                "field": (
                                    "source_account_id"
                                    if movement_type == "IN"
                                    else "target_account_id"
                                ),
                                "code": "required",
                            }
                        ]
                    },
                )
            else:
                counter_account_id = request.counter_account_id
            drawer = await self._accounting.inventory_account(
                session, branch_id=request.branch_id, currency_id=currency.id
            )
            if counter_account_id == drawer.id:
                raise CashCounterAccountRequiredError(
                    "A cash movement needs a counter account other than the drawer itself.",
                    details={
                        "fields": [{"field": "counter_account_id", "code": "same_as_cash_account"}],
                        "cash_account_id": str(drawer.id),
                    },
                )

            # Its own document id: one journal entry per movement is what
            # ``ux_journal_entries_one_per_reference`` allows, and it keeps the reversal of
            # one movement from touching any other.
            reference_id = uuid.uuid4()
            entry = await self._accounting.post_cash_movement(
                movement_type=movement_type,
                reference_id=reference_id,
                branch_id=request.branch_id,
                cash_account_id=drawer.id,
                counter_account_id=counter_account_id,
                currency_id=currency.id,
                amount=amount,
                # The ledger values the movement: receiving enters at the house's quote,
                # disposing leaves at the drawer's carrying rate, and a disposal the position
                # cannot cover is refused under the account lock instead of at COMMIT.
                resolve_rate=True,
                adjustment_sign=request.adjustment_sign,
                description=request.description or request.reason,
                transaction_date=request.transaction_date,
                device_id=request.device_id,
                actor=actor,
                session=session,
            )
            movement_ids = await self._accounting.record_cash_movements(
                session,
                reference_type=REFERENCE_TYPE_CASH,
                reference_id=reference_id,
                branch_id=request.branch_id,
                movements=[
                    CashMovementSpec(
                        account_id=drawer.id,
                        currency_id=currency.id,
                        movement_type=movement_type,
                        amount=amount,
                        description=request.description or request.reason,
                        adjustment_sign=request.adjustment_sign,
                    )
                ],
                actor=actor,
                journal_entry_id=entry.id,
                cash_session_id=cash_session.id,
                device_id=request.device_id,
                client_event_id=request.client_event_id,
            )
            movement_id = movement_ids[0]
            AuditService(session).record(
                action=audit_action,
                entity_type="cash_movement",
                entity_id=movement_id,
                new_data={
                    "branch_id": str(request.branch_id),
                    "session_id": str(cash_session.id),
                    "account_id": str(drawer.id),
                    "account_code": drawer.code,
                    "currency_id": str(currency.id),
                    "currency_code": currency.code,
                    "movement_type": movement_type,
                    "amount": format_decimal(amount),
                    "adjustment_sign": request.adjustment_sign,
                    "reason": request.reason,
                    "reference_id": str(reference_id),
                    "journal_entry_id": str(entry.id),
                    "counter_account_id": str(counter_account_id),
                    "client_event_id": (
                        str(request.client_event_id) if request.client_event_id else None
                    ),
                },
                actor=actor,
            )
            payload = (await self._movement_view(session, movement_id)).to_payload()
            self._complete(guard, status_code=201, payload=payload, resource_id=movement_id)

        return CashResult(payload=payload, status_code=201, movement_id=movement_id)

    # ==================================================================== closing
    async def close_session(
        self,
        *,
        actor: ActorContext,
        request: CloseSessionRequest,
        idempotency_key: uuid.UUID,
    ) -> CashResult:
        """Close a shift against a physical count (§9.4 ``POST /cash/close``).

        The count is compared with the amount the shift's own immutable movements imply. A
        difference is real money, so it is posted to ``5090 Cash Short / Over`` under
        ``cash.adjust`` — and if the operator does not hold that permission the shift stays
        open rather than being closed with an unexplained difference.
        """
        await self._authorize(actor, "close", context={"session_id": str(request.session_id)})
        counted = self._validate_counts(request.counted)

        async with self._accounting.document_transaction() as session:
            sessions = CashSessionRepository(session)
            # Lock the shift first: no movement can be recorded while it closes, and a second
            # close queues behind this one and then sees ``CLOSED`` (never a double close).
            cash_session = await sessions.get(request.session_id, for_update=True)
            if cash_session is None or not self._accounting.branch_in_scope(
                actor, cash_session.branch_id
            ):
                raise ResourceNotFoundError(
                    "That cash session does not exist.",
                    details={"resource": "cash_session", "id": str(request.session_id)},
                )
            await self._accounting.assert_branch_scope(
                actor, branch_id=cash_session.branch_id, context={"operation": "cash.close"}
            )
            if cash_session.status != STATUS_OPEN:
                raise CashSessionNotOpenError(
                    "That cash session is already closed.",
                    details={
                        "session_id": str(cash_session.id),
                        "status": cash_session.status,
                        "closed_at": _iso(cash_session.closed_at),
                    },
                )
            await self._assert_session_owner(actor=actor, cash_session=cash_session)
            guard = await self._claim(
                session,
                actor=actor,
                key=idempotency_key,
                endpoint=ENDPOINT_CASH_CLOSE,
                fingerprint=request.fingerprint(),
            )
            if isinstance(guard, CashResult):
                return guard

            totals = await sessions.movement_totals(cash_session.id)
            lines = {
                _uuid(line["currency_id"]): line for line in await sessions.lines(cash_session.id)
            }
            nets: dict[uuid.UUID, Decimal] = {}
            for total in totals:
                # The declared opening already includes the cash the ``OPENING`` movement put
                # on the books (§6.1 posts the count), so the expectation adds the shift's
                # *other* movements to it — in the carried case there is no opening movement
                # at all and this is simply the shift's net.
                nets[_uuid(total["currency_id"])] = Decimal(str(total["movement_sum"])) - Decimal(
                    str(total["opening_sum"])
                )
            for currency_id in lines:
                nets.setdefault(currency_id, Decimal(0))
            missing = sorted(str(item) for item in nets if item not in counted)
            if missing:
                raise CashReconciliationIncompleteError(
                    "Every currency the shift touched has to be counted before it closes.",
                    details={
                        "session_id": str(cash_session.id),
                        "missing_currency_ids": missing,
                        "hint": "Count the drawer per currency; the count is the only evidence.",
                    },
                )
            unknown = sorted(str(item) for item in counted if item not in nets)
            if unknown:
                raise ValidationError(
                    "That currency did not move in this shift, so it cannot be counted here.",
                    details={
                        "session_id": str(cash_session.id),
                        "fields": [
                            {"field": "counted[].currency_id", "code": "not_in_session"}
                            for _ in unknown
                        ],
                        "currency_ids": unknown,
                    },
                )

            variances: list[dict[str, Any]] = []
            for currency_id in sorted(nets, key=str):
                currency = await self._require_currency(
                    session, currency_id, "counted[].currency_id"
                )
                counted_amount = self._check_money(
                    counted[currency_id], currency=currency, field="counted[].amount"
                )
                line = await sessions.line_for(session_id=cash_session.id, currency_id=currency_id)
                if line is None:
                    # A currency the shift traded that nobody declared at open: its opening is
                    # therefore the position the drawer held before the shift moved anything.
                    position = await self._carried_position(
                        session, branch_id=cash_session.branch_id, currency_id=currency_id
                    )
                    line = CashSessionLine(
                        cash_session_id=cash_session.id,
                        currency_id=currency_id,
                        opening_declared=money_difference(
                            position.physical_balance, nets[currency_id]
                        ),
                    )
                    sessions.add_line(line)
                    await sessions.flush()
                expected = money_sum_declared(
                    Decimal(str(line.opening_declared)), nets[currency_id]
                )
                position = await self._carried_position(
                    session, branch_id=cash_session.branch_id, currency_id=currency_id
                )
                if expected != position.physical_balance:
                    raise DataIntegrityError(
                        "The drawer holds an amount this shift's own movements do not explain.",
                        details={
                            "reason": "UNEXPLAINED_DRAWER_POSITION",
                            "session_id": str(cash_session.id),
                            "currency_id": str(currency_id),
                            "currency_code": currency.code,
                            "expected": format_decimal(expected),
                            "physical_balance": format_decimal(position.physical_balance),
                        },
                    )
                difference = money_difference(counted_amount, expected)
                line.expected_amount = expected
                line.counted_amount = counted_amount
                await sessions.flush()
                if difference == 0:
                    continue
                await self._require_permission(
                    actor,
                    Permission.CASH_ADJUST,
                    operation="close.variance",
                    context={
                        "session_id": str(cash_session.id),
                        "currency_code": currency.code,
                        "difference": format_decimal(difference),
                    },
                )
                drawer = await self._accounting.inventory_account(
                    session, branch_id=cash_session.branch_id, currency_id=currency_id
                )
                adjustment = await self._post_variance(
                    session,
                    actor=actor,
                    cash_session=cash_session,
                    drawer=drawer,
                    currency=currency,
                    difference=difference,
                )
                variances.append(
                    {
                        "currency_id": str(currency_id),
                        "currency_code": currency.code,
                        "opening_declared": format_decimal(Decimal(str(line.opening_declared))),
                        "expected_amount": format_decimal(expected),
                        "counted_amount": format_decimal(counted_amount),
                        "difference": format_decimal(difference),
                        "movement_id": str(adjustment.movement_id),
                        "journal_entry_id": (
                            str(adjustment.journal_entry_id)
                            if adjustment.journal_entry_id
                            else None
                        ),
                    }
                )

            cash_session.status = STATUS_CLOSED
            cash_session.closed_by = actor.user_id
            cash_session.closed_at = dt.datetime.now(dt.UTC)
            if request.notes:
                cash_session.notes = request.notes
            await sessions.flush()

            AuditService(session).record(
                action=AuditAction.CASH_SESSION_CLOSED,
                entity_type="cash_session",
                entity_id=cash_session.id,
                old_data={"status": STATUS_OPEN},
                new_data={
                    "status": STATUS_CLOSED,
                    "branch_id": str(cash_session.branch_id),
                    "device_id": str(cash_session.device_id) if cash_session.device_id else None,
                    "variances": variances,
                    "notes": cash_session.notes,
                },
                actor=actor,
            )
            payload = (
                await self._load_session(session, cash_session.id, with_movements=True)
            ).to_payload()
            payload["variances"] = variances
            self._complete(guard, status_code=200, payload=payload, resource_id=cash_session.id)

        return CashResult(
            payload=payload, status_code=200, session_id=cash_session.id, replayed=False
        )

    # =================================================================== reversal
    async def reverse_movement(
        self,
        *,
        actor: ActorContext,
        movement_id: uuid.UUID,
        reason: str,
        idempotency_key: uuid.UUID | None = None,
    ) -> CashResult:
        """Reverse one cash movement with a compensating entry (§9.4, PART 22).

        The original movement and its journal entry stay exactly as posted; a mirror entry and
        one opposite physical movement are written, and a second reversal of the same movement
        is refused. A movement that belongs to another document (an exchange deal, a shift
        opening) is refused as not reversible here: that document owns its own correction.
        """
        if not reason or not reason.strip():
            raise ValidationError(
                "A reversal needs a reason: the auditor reads it.",
                details={"fields": [{"field": "reason", "code": "required"}]},
            )
        await self._authorize(actor, "reverse", context={"movement_id": str(movement_id)})

        async with self._accounting.document_transaction() as session:
            movements = CashMovementRepository(session)
            original = await movements.row(movement_id)
            if original is None or not self._accounting.branch_in_scope(
                actor, original["branch_id"]
            ):
                raise ResourceNotFoundError(
                    "That cash movement does not exist.",
                    details={"resource": "cash_movement", "id": str(movement_id)},
                )
            await self._accounting.assert_branch_scope(
                actor, branch_id=original["branch_id"], context={"operation": "cash.reverse"}
            )
            if str(original["reference_type"]) not in REVERSIBLE_REFERENCE_TYPES:
                raise CashMovementNotReversibleError(
                    "That cash movement belongs to another document and cannot be reversed "
                    "on its own.",
                    details={
                        "movement_id": str(movement_id),
                        "reference_type": original["reference_type"],
                        "reference_id": (
                            str(original["reference_id"]) if original["reference_id"] else None
                        ),
                        "reversible_reference_types": list(REVERSIBLE_REFERENCE_TYPES),
                    },
                )
            # Serialise corrections of the same movement: the second caller must see the
            # first one's compensating row, not a snapshot taken before it committed.
            await movements.lock(movement_id)
            existing = await movements.reversal_of(movement_id)
            if existing is not None:
                raise AlreadyReversedError(
                    "This cash movement has already been reversed.",
                    details={
                        "movement_id": str(movement_id),
                        "reversal_movement_id": str(existing["id"]),
                        "reversed_at": _iso(existing["created_at"]),
                    },
                )
            guard = await self._claim(
                session,
                actor=actor,
                key=idempotency_key,
                endpoint=ENDPOINT_CASH_REVERSE,
                fingerprint={"movement_id": movement_id, "reason": reason.strip()},
            )
            if isinstance(guard, CashResult):
                return guard

            movement_type = str(original["movement_type"])
            direction = MIRROR_MOVEMENT.get(movement_type)
            if direction is None:  # pragma: no cover - every stored type is in the table
                raise CashMovementNotReversibleError(
                    "That movement has no opposite this module can record.",
                    details={"movement_id": str(movement_id), "movement_type": movement_type},
                )
            amount = Decimal(str(original["amount"]))
            adjustment_sign = (
                -int(original["adjustment_sign"]) if movement_type == "ADJUSTMENT" else None
            )
            await self._assert_reversible_position(
                session,
                original=original,
                direction=direction,
                adjustment_sign=adjustment_sign,
                amount=amount,
            )
            account_id = _uuid(original["account_id"])
            currency_id = _uuid(original["currency_id"])
            branch_id = _uuid(original["branch_id"])
            # The evidence chain is read before it is mirrored. The entry this movement
            # points at must post the drawer (its other leg is the counter account); a
            # broken link is a defect to report, not a correction to post on trust.
            await self._counter_account_of(session, original=original)
            # The undo is the *ledger's* mirror of the original entry — exactly what the
            # exchange reversal posts (Phase 5 §6.7). Posting a movement-shaped entry and
            # reversing that one instead would move the books twice while the drawer moved
            # once: the till and the ledger would disagree by the amount for ever.
            entry = await self._accounting.reverse_journal_entry(
                journal_entry_id=_uuid(original["journal_entry_id"]),
                reason=reason.strip(),
                actor=actor,
                device_id=actor.device_id,
                session=session,
                authority=Permission.CASH_ADJUST,
            )
            compensating_ids = await self._accounting.record_cash_movements(
                session,
                reference_type=REFERENCE_TYPE_REVERSAL,
                reference_id=movement_id,
                branch_id=branch_id,
                movements=[
                    CashMovementSpec(
                        account_id=account_id,
                        currency_id=currency_id,
                        movement_type=direction,
                        amount=amount,
                        description=f"Reversal of movement {movement_id}",
                        adjustment_sign=adjustment_sign,
                    )
                ],
                actor=actor,
                journal_entry_id=entry.id,
                cash_session_id=(
                    _uuid(original["cash_session_id"]) if original["cash_session_id"] else None
                ),
                device_id=actor.device_id,
                allow_inactive_branch=True,
            )
            compensating_id = compensating_ids[0]
            AuditService(session).record(
                action=AuditAction.CASH_MOVEMENT_REVERSED,
                entity_type="cash_movement",
                entity_id=movement_id,
                old_data={
                    "movement_type": movement_type,
                    "amount": format_decimal(amount),
                    "adjustment_sign": original["adjustment_sign"],
                    "account_id": str(account_id),
                    "currency_id": str(currency_id),
                    "journal_entry_id": (
                        str(original["journal_entry_id"]) if original["journal_entry_id"] else None
                    ),
                },
                new_data={
                    "reason": reason.strip(),
                    "reversal_movement_id": str(compensating_id),
                    "movement_type": direction,
                    "adjustment_sign": adjustment_sign,
                    "journal_entry_id": str(entry.id),
                },
                actor=actor,
            )
            payload = (await self._movement_view(session, compensating_id)).to_payload()
            self._complete(guard, status_code=200, payload=payload, resource_id=compensating_id)

        return CashResult(payload=payload, status_code=200, movement_id=compensating_id)

    # ====================================================================== reads
    async def balance(
        self, *, actor: ActorContext, branch_id: uuid.UUID | None = None
    ) -> list[CashBalanceRow]:
        """Per branch and currency: what the drawers hold and what the books carry (§9.4).

        The two numbers come from the two immutable sources the accounting model names:
        ``v_cash_position`` (the movements) and the journal lines of the chart's cash band. A
        row where they disagree carries ``reconciled: false`` — the module reports the
        disagreement rather than smoothing it away, because only an adjustment (with a
        reason, a permission and an audit row) may change either number.
        """
        await self._authorize(
            actor, "view", context={"branch_id": str(branch_id) if branch_id else None}
        )
        branch_ids = await self._accounting.branch_scope_filter(actor, branch_id=branch_id)
        async with self._database.session() as session:
            rows = CashSessionRepository(session)
            physical = {
                (str(row["branch_id"]), str(row["currency_id"])): row
                for row in await rows.position_rows(branch_ids=branch_ids)
            }
            ledger = {
                (str(row["branch_id"]), str(row["currency_id"])): row
                for row in await rows.ledger_position_rows(
                    branch_ids=branch_ids, band_pattern=CASH_ACCOUNT_CODE_PATTERN
                )
            }
            positions: list[CashBalanceRow] = []
            for key in sorted(set(physical) | set(ledger)):
                row = physical.get(key) or ledger[key]
                ledger_row = ledger.get(key) or {}
                physical_row = physical.get(key) or {}
                positions.append(
                    CashBalanceRow(
                        branch_id=_uuid(row["branch_id"]),
                        branch_code=str(row["branch_code"]),
                        currency_id=_uuid(row["currency_id"]),
                        currency_code=str(
                            physical_row.get("currency_code")
                            or ledger_row.get("currency_code")
                            or ""
                        ),
                        physical_balance=Decimal(str(physical_row.get("balance", 0))),
                        ledger_functional_balance=Decimal(
                            str(ledger_row.get("functional_balance", 0))
                        ),
                        ledger_quantity=Decimal(str(ledger_row.get("quantity", 0))),
                    )
                )
            return positions

    async def get_session(self, *, actor: ActorContext, session_id: uuid.UUID) -> CashSessionView:
        """One shift with its lines and movements (``GET /cash/sessions/{id}``)."""
        await self._authorize(actor, "view", context={"session_id": str(session_id)})
        async with self._database.session() as session:
            view = await self._load_session(session, session_id, with_movements=True)
        if not self._accounting.branch_in_scope(actor, view.branch_id):
            raise ResourceNotFoundError(
                "That cash session does not exist.",
                details={"resource": "cash_session", "id": str(session_id)},
            )
        return view

    async def current_session(
        self,
        *,
        actor: ActorContext,
        branch_id: uuid.UUID | None = None,
        device_id: uuid.UUID | None = None,
    ) -> CashSessionView | None:
        """The open shift of a drawer, or ``None`` (``GET /cash/sessions/current``).

        ``device_id`` defaults to the caller's device: this is the call a client makes when it
        comes back online and has to know whether its till is still open.
        """
        await self._authorize(
            actor, "view", context={"branch_id": str(branch_id) if branch_id else None}
        )
        scope = branch_id if branch_id is not None else actor.branch_id
        if scope is None:
            raise PermissionDeniedError(
                "Reading the current cash session needs a branch.",
                details={"reason": "BRANCH_REQUIRED"},
            )
        await self._accounting.assert_branch_scope(
            actor, branch_id=scope, context={"operation": "cash.session.current"}
        )
        device = device_id if device_id is not None else actor.device_id
        async with self._database.session() as session:
            row = await CashSessionRepository(session).find_open(branch_id=scope, device_id=device)
            if row is None:
                return None
            return await self._load_session(session, row.id, with_movements=False)

    async def list_sessions(
        self,
        *,
        actor: ActorContext,
        branch_id: uuid.UUID | None = None,
        status: str | None = None,
        device_id: uuid.UUID | None = None,
        opened_by: uuid.UUID | None = None,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[CashSessionView], int]:
        """Shift history with its differences (``GET /cash/sessions``)."""
        await self._authorize(
            actor, "view", context={"branch_id": str(branch_id) if branch_id else None}
        )
        if status is not None and status not in (STATUS_OPEN, STATUS_CLOSED):
            raise ValidationError(
                "That is not a cash session status.",
                details={
                    "fields": [{"field": "status", "code": "unsupported"}],
                    "allowed": [STATUS_OPEN, STATUS_CLOSED],
                },
            )
        branch_ids = await self._accounting.branch_scope_filter(actor, branch_id=branch_id)
        async with self._database.session() as session:
            rows = CashSessionRepository(session)
            page, total = await rows.list_sessions(
                branch_ids=branch_ids,
                status=status,
                device_id=device_id,
                opened_by=opened_by,
                from_=from_,
                to=to,
                limit=limit,
                offset=offset,
            )
            ids = [_uuid(row["id"]) for row in page]
            lines = await rows.lines_for(ids)
            adjustments = await self._adjustment_entries(session, ids)
            return [
                self._session_view(
                    row,
                    lines=lines.get(_uuid(row["id"]), []),
                    adjustment_entry_ids=adjustments.get(_uuid(row["id"]), {}),
                )
                for row in page
            ], total

    async def list_movements(
        self,
        *,
        actor: ActorContext,
        branch_id: uuid.UUID | None = None,
        currency_id: uuid.UUID | None = None,
        movement_type: str | None = None,
        session_id: uuid.UUID | None = None,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[CashMovementView], int]:
        """The cash movement book, newest first (``GET /cash/movements``)."""
        await self._authorize(
            actor, "view", context={"branch_id": str(branch_id) if branch_id else None}
        )
        if movement_type is not None and movement_type not in (
            *DIRECT_MOVEMENT_TYPES,
            "OPENING",
            "EXPENSE",
            "CLOSING",
        ):
            raise ValidationError(
                "That is not a cash movement type.",
                details={"fields": [{"field": "movement_type", "code": "unsupported"}]},
            )
        branch_ids = await self._accounting.branch_scope_filter(actor, branch_id=branch_id)
        async with self._database.session() as session:
            movements = CashMovementRepository(session)
            page, total = await movements.list_movements(
                branch_ids=branch_ids,
                currency_id=currency_id,
                movement_type=movement_type,
                session_id=session_id,
                from_=from_,
                to=to,
                limit=limit,
                offset=offset,
            )
            views = [
                CashMovementView(
                    row=row, reversal_movement_id=await self._reversal_id(session, row)
                )
                for row in page
            ]
            return views, total

    async def get_movement(
        self, *, actor: ActorContext, movement_id: uuid.UUID
    ) -> CashMovementView:
        """One movement, with the movement that reversed it when there is one."""
        await self._authorize(actor, "view", context={"movement_id": str(movement_id)})
        async with self._database.session() as session:
            view = await self._movement_view(session, movement_id)
        if not self._accounting.branch_in_scope(actor, view.row["branch_id"]):
            raise ResourceNotFoundError(
                "That cash movement does not exist.",
                details={"resource": "cash_movement", "id": str(movement_id)},
            )
        return view

    # ================================================================== internals
    async def _post_opening(
        self,
        session: AsyncSession,
        *,
        actor: ActorContext,
        branch_id: uuid.UUID,
        cash_session: CashSession,
        drawer: Account,
        currency: Currency,
        amount: Decimal,
        exchange_rate: Decimal | None,
        device_id: uuid.UUID | None,
    ) -> None:
        """Post one currency's opening balance: ``Dr Cash / Cr 6000`` (§6.1), with its movement.

        The rate is the one the model says is *captured in the request*; when the request does
        not name one, the ledger resolves the house's own quote for the currency, so an opening
        never depends on a number nobody recorded.
        """
        reference_id = uuid.uuid4()
        entry = await self._accounting.post_cash_movement(
            movement_type="OPENING",
            reference_id=reference_id,
            branch_id=branch_id,
            cash_account_id=drawer.id,
            counter_account_id=await self._account_by_code(
                session, code=OPENING_OFFSET_ACCOUNT_CODE, field="counter_account_id"
            ),
            currency_id=currency.id,
            amount=amount,
            exchange_rate=exchange_rate or Decimal(1),
            resolve_rate=exchange_rate is None,
            description=f"Opening balance for cash session {cash_session.id}",
            device_id=device_id,
            actor=actor,
            session=session,
        )
        await self._accounting.record_cash_movements(
            session,
            reference_type=REFERENCE_TYPE_OPENING,
            reference_id=reference_id,
            branch_id=branch_id,
            movements=[
                CashMovementSpec(
                    account_id=drawer.id,
                    currency_id=currency.id,
                    movement_type="OPENING",
                    amount=amount,
                    description=f"Opening balance for cash session {cash_session.id}",
                )
            ],
            actor=actor,
            journal_entry_id=entry.id,
            cash_session_id=cash_session.id,
            device_id=device_id,
        )

    async def _post_variance(
        self,
        session: AsyncSession,
        *,
        actor: ActorContext,
        cash_session: CashSession,
        drawer: Account,
        currency: Currency,
        difference: Decimal,
    ) -> CashResult:
        """Post a shift's short/over as an ``ADJUSTMENT`` against ``5090`` (§6.4).

        The sign is the sign of the difference: a shortage is a disposal, an overage is a
        receipt. ``amount`` is the magnitude, because the movement table stores a quantity and
        a direction, and the database refuses anything else.
        """
        adjustment_sign = 1 if difference > 0 else -1
        amount = difference if difference > 0 else -difference
        reference_id = uuid.uuid4()
        entry = await self._accounting.post_cash_movement(
            movement_type="ADJUSTMENT",
            reference_id=reference_id,
            branch_id=cash_session.branch_id,
            cash_account_id=drawer.id,
            counter_account_id=await self._account_by_code(
                session, code=SHORT_OVER_ACCOUNT_CODE, field="counter_account_id"
            ),
            currency_id=currency.id,
            amount=amount,
            resolve_rate=True,
            adjustment_sign=adjustment_sign,
            description=f"Cash count difference at close of session {cash_session.id}",
            device_id=cash_session.device_id,
            actor=actor,
            session=session,
        )
        movement_ids = await self._accounting.record_cash_movements(
            session,
            reference_type=REFERENCE_TYPE_CASH,
            reference_id=reference_id,
            branch_id=cash_session.branch_id,
            movements=[
                CashMovementSpec(
                    account_id=drawer.id,
                    currency_id=currency.id,
                    movement_type="ADJUSTMENT",
                    amount=amount,
                    description=f"Cash count difference at close of session {cash_session.id}",
                    adjustment_sign=adjustment_sign,
                )
            ],
            actor=actor,
            journal_entry_id=entry.id,
            cash_session_id=cash_session.id,
            device_id=cash_session.device_id,
        )
        AuditService(session).record(
            action=AuditAction.CASH_ADJUSTMENT_RECORDED,
            entity_type="cash_movement",
            entity_id=movement_ids[0],
            new_data={
                "branch_id": str(cash_session.branch_id),
                "session_id": str(cash_session.id),
                "account_id": str(drawer.id),
                "account_code": drawer.code,
                "currency_id": str(currency.id),
                "currency_code": currency.code,
                "movement_type": "ADJUSTMENT",
                "amount": format_decimal(amount),
                "adjustment_sign": adjustment_sign,
                "reason": "CASH_COUNT_DIFFERENCE",
                "reference_id": str(reference_id),
                "journal_entry_id": str(entry.id),
            },
            actor=actor,
        )
        return CashResult(
            payload={},
            status_code=201,
            session_id=cash_session.id,
            movement_id=movement_ids[0],
            journal_entry_id=entry.id,
        )

    async def _assert_reversible_position(
        self,
        session: AsyncSession,
        *,
        original: Mapping[str, Any],
        direction: str,
        adjustment_sign: int | None,
        amount: Decimal,
    ) -> None:
        """Refuse a correction that would take the branch's till negative.

        The mirror of an ``IN``/``OPENING`` takes money out (``OUT``), and the mirror of an
        ``ADJUSTMENT`` takes it back the other way. ``ct_cash_movements_non_negative``
        (``NEX01``) is the authority and would refuse this at COMMIT; saying it here turns a
        database error into an answer an operator can act on (409 with the shortfall), exactly
        as the ledger does for a disposal it cannot cover.
        """
        disposing = direction == "OUT" or (direction == "ADJUSTMENT" and (adjustment_sign or 0) < 0)
        if not disposing:
            return
        position = await self._carried_position(
            session,
            branch_id=_uuid(original["branch_id"]),
            currency_id=_uuid(original["currency_id"]),
        )
        if position.physical_balance < amount:
            raise InsufficientBalanceError(
                "Reversing that movement would take this drawer below zero.",
                details={
                    "branch_id": str(original["branch_id"]),
                    "currency_id": str(original["currency_id"]),
                    "movement_id": str(original["id"]),
                    "available": format_decimal(position.physical_balance),
                    "required": format_decimal(amount),
                    "shortfall": format_decimal(
                        money_difference(amount, position.physical_balance)
                    ),
                    "reason": (
                        "NO_POSITION" if position.physical_balance == 0 else "QUANTITY_EXCEEDED"
                    ),
                },
            )

    async def _counter_account_of(
        self, session: AsyncSession, *, original: Mapping[str, Any]
    ) -> uuid.UUID:
        """The counter account a movement's own journal entry used, read from its lines.

        A correcting entry must use the accounts the original used, not a resolution taken
        again today: the chart may have gained an account since, and a reversal has to undo
        what was posted.
        """
        entry_id = original["journal_entry_id"]
        if entry_id is None:  # pragma: no cover - a cash movement is always posted with a plan
            raise DataIntegrityError(
                "That cash movement has no journal entry to mirror.",
                details={"movement_id": str(original["id"]), "reason": "NO_JOURNAL_ENTRY"},
            )
        lines = await CashMovementRepository(session).entry_lines(_uuid(entry_id))
        cash_account_id = _uuid(original["account_id"])
        for line in lines:
            account_id = _uuid(line["account_id"])
            if account_id != cash_account_id:
                return account_id
        raise DataIntegrityError(  # pragma: no cover - a cash entry always has two legs
            "That cash movement's entry has no counter account to mirror.",
            details={"movement_id": str(original["id"]), "entry_id": str(entry_id)},
        )

    def _validate_openings(self, openings: Sequence[CashOpening]) -> list[CashOpening]:
        """Reject a malformed opening list before any lock is taken."""
        seen: set[uuid.UUID] = set()
        for index, opening in enumerate(openings):
            if opening.currency_id in seen:
                raise ValidationError(
                    "A currency can only be counted once when a shift opens.",
                    details={
                        "fields": [{"field": f"openings[{index}].currency_id", "code": "duplicate"}]
                    },
                )
            seen.add(opening.currency_id)
            if opening.exchange_rate is not None:
                require_positive_money(
                    opening.exchange_rate, field=f"openings[{index}].exchange_rate"
                )
        return list(openings)

    def _validate_counts(self, counted: Sequence[CashCount]) -> dict[uuid.UUID, Decimal]:
        """Reject a malformed count before any lock is taken (same rule, other endpoint)."""
        amounts: dict[uuid.UUID, Decimal] = {}
        for index, item in enumerate(counted):
            if item.currency_id in amounts:
                raise ValidationError(
                    "A currency can only be counted once when a shift closes.",
                    details={
                        "fields": [{"field": f"counted[{index}].currency_id", "code": "duplicate"}]
                    },
                )
            amounts[item.currency_id] = item.amount
        return amounts

    def _check_money(self, value: Decimal, *, currency: Currency, field: str) -> Decimal:
        """The ledger's money rules, applied to the request before anything is written."""
        # ``validate_money`` is the ledger's own validator (``NUMERIC(30,10)``, exactly), so
        # the scale and the bound are decided once, in one place: a value the books could not
        # store is refused here as a bad request instead of surfacing from the insert.
        amount = validate_money(value, field=field)
        if amount < 0:
            raise ValidationError(
                "A cash amount cannot be negative; state the direction instead.",
                details={"fields": [{"field": field, "code": "negative"}]},
            )
        if currency.decimal_places < MONEY_SCALE and amount != quantize_money(
            amount, currency.decimal_places
        ):
            raise ValidationError(
                "That amount cannot be expressed in the currency's smallest unit.",
                details={
                    "fields": [{"field": field, "code": "below_smallest_unit"}],
                    "currency_code": currency.code,
                    "decimal_places": currency.decimal_places,
                },
            )
        return amount

    async def _require_branch(self, branch: Branch | None) -> Branch:
        if branch is None:
            raise ResourceNotFoundError(
                "That branch does not exist.",
                details={"fields": [{"field": "branch_id", "code": "not_found"}]},
            )
        if not branch.is_active:
            raise ValidationError(
                "A closed branch cannot open a cash session.",
                details={"fields": [{"field": "branch_id", "code": "inactive"}]},
            )
        return branch

    async def _require_currency(
        self, session: AsyncSession, currency_id: uuid.UUID, field: str
    ) -> Currency:
        currency = await CurrencyRepository(session).get(currency_id)
        if currency is None:
            raise ResourceNotFoundError(
                "That currency does not exist.",
                details={"fields": [{"field": field, "code": "not_found"}]},
            )
        if not currency.is_active:
            raise CurrencyInactiveError(details={"fields": [{"field": field, "code": "inactive"}]})
        return currency

    async def _account_by_code(self, session: AsyncSession, *, code: str, field: str) -> uuid.UUID:
        """Resolve a chart account the accounting model names by its code."""
        account = await AccountRepository(session).get_by_code(code)
        if account is None:
            raise DataIntegrityError(
                "The chart of accounts is missing an account the accounting model requires.",
                details={
                    "reason": "MISSING_CHART_ACCOUNT",
                    "account_code": code,
                    "field": field,
                },
            )
        if not account.is_active or not account.is_postable:
            raise ConflictError(
                "A chart account this operation needs is not postable.",
                details={
                    "reason": "ACCOUNT_NOT_POSTABLE",
                    "account_code": code,
                    "is_active": bool(account.is_active),
                    "is_postable": bool(account.is_postable),
                },
            )
        return account.id

    async def _resolve_device(
        self,
        session: AsyncSession,
        *,
        actor: ActorContext,
        device_id: uuid.UUID | None,
        branch_id: uuid.UUID,
    ) -> uuid.UUID | None:
        """The device the shift is bound to: the caller's own, or an explicitly named one.

        A device is *optional* — a till can be opened from a shared terminal — but when one is
        named it must belong to the branch being opened: a shift is a physical drawer, and a
        device at another branch is a different drawer.
        """
        candidate = device_id or actor.device_id
        if candidate is None:
            return None
        device: Device | None = await DeviceRepository(session).get_device(candidate)
        if device is None:
            raise ResourceNotFoundError(
                "That device does not exist.",
                details={"fields": [{"field": "device_id", "code": "not_found"}]},
            )
        if device.branch_id != branch_id:
            raise ValidationError(
                "That device belongs to another branch.",
                details={
                    "fields": [{"field": "device_id", "code": "branch_mismatch"}],
                    "device_branch_id": str(device.branch_id),
                },
            )
        return device.id

    async def _open_session_at(
        self, session: AsyncSession, *, branch_id: uuid.UUID
    ) -> CashSession | None:
        """Any open shift at a branch (one at a time, by design — see the module docstring)."""
        rows = await CashSessionRepository(session).open_sessions(branch_id=branch_id)
        return rows[0] if rows else None

    async def _require_open_session(
        self, session: AsyncSession, *, actor: ActorContext, request: CashMovementRequest
    ) -> CashSession:
        """The shift a movement belongs to: the named one, or the caller's open drawer.

        Locked before anything else is decided, so a movement can never be recorded into a
        shift that is closing, and a closed shift can never receive one.
        """
        sessions = CashSessionRepository(session)
        if request.session_id is not None:
            cash_session = await sessions.get(request.session_id, for_update=True)
            if cash_session is None or not self._accounting.branch_in_scope(
                actor, cash_session.branch_id
            ):
                raise ResourceNotFoundError(
                    "That cash session does not exist.",
                    details={"resource": "cash_session", "id": str(request.session_id)},
                )
            if cash_session.branch_id != request.branch_id:
                raise ValidationError(
                    "That cash session belongs to another branch.",
                    details={
                        "fields": [{"field": "session_id", "code": "branch_mismatch"}],
                        "session_branch_id": str(cash_session.branch_id),
                    },
                )
        else:
            candidate = request.device_id or actor.device_id
            found = await sessions.find_open(branch_id=request.branch_id, device_id=candidate)
            if found is None:
                # A device-bound caller may still work the branch's shift when the shift has no
                # device (a shared terminal is exactly that case); anything else is an operator
                # trying to move cash with no shift open.
                found = await sessions.find_open(branch_id=request.branch_id, device_id=None)
            if found is None:
                raise CashSessionNotOpenError(
                    "There is no open cash session for this drawer.",
                    details={
                        "branch_id": str(request.branch_id),
                        "device_id": str(candidate) if candidate else None,
                        "hint": "Open a cash session before recording movements.",
                    },
                )
            cash_session = await sessions.get(found.id, for_update=True)
            if cash_session is None:  # pragma: no cover - the row was just read
                raise CashSessionNotOpenError(
                    "There is no open cash session for this drawer.",
                    details={"branch_id": str(request.branch_id)},
                )
        if cash_session.status != STATUS_OPEN:
            raise CashSessionNotOpenError(
                "That cash session is closed.",
                details={
                    "session_id": str(cash_session.id),
                    "status": cash_session.status,
                    "closed_at": _iso(cash_session.closed_at),
                },
            )
        return cash_session

    async def _assert_session_owner(
        self, *, actor: ActorContext, cash_session: CashSession
    ) -> None:
        """A shift is closed by the operator who opened it, or by a supervisory authority.

        ``cash.close`` is held by cashiers for *their own* shift (API_CONTRACT §8); closing
        somebody else's till is a supervisory act, and ``cash.adjust`` — the permission the
        contract reserves for corrections, which cashiers do not hold — is what authorises it.
        """
        if cash_session.opened_by == actor.user_id:
            return
        if str(Permission.CASH_ADJUST) in actor.permissions:
            return
        raise PermissionDeniedError(
            "You can only close the cash session you opened.",
            details={
                "reason": "NOT_SESSION_OWNER",
                "session_id": str(cash_session.id),
                "opened_by": str(cash_session.opened_by),
                "hint": "A manager, accountant or owner can close a shift they did not open.",
            },
        )

    async def _carried_position(
        self, session: AsyncSession, *, branch_id: uuid.UUID, currency_id: uuid.UUID
    ) -> CashBalanceRow:
        """One (branch, currency)'s position: the movements beside the ledger.

        The ledger side is read for **the drawer this operation will actually post to**
        (``AccountingService.inventory_account`` — the same resolution the posting itself
        uses), not for the chart's cash band as a whole. The difference matters exactly when a
        chart has two candidates for one currency at one branch: a control that sums them
        would report "reconciled" for a branch whose till the posting code cannot even pick.
        """
        rows = CashSessionRepository(session)
        physical = {
            (str(row["branch_id"]), str(row["currency_id"])): row
            for row in await rows.position_rows(branch_ids=[branch_id])
        }
        key = (str(branch_id), str(currency_id))
        physical_row = physical.get(key) or {}
        drawer = await self._accounting.inventory_account(
            session, branch_id=branch_id, currency_id=currency_id
        )
        ledger_row = await rows.ledger_account_positions(
            branch_id=branch_id, account_ids=[drawer.id]
        )
        return CashBalanceRow(
            branch_id=branch_id,
            branch_code=str(physical_row.get("branch_code", "")),
            currency_id=currency_id,
            currency_code=str(physical_row.get("currency_code", "")),
            physical_balance=Decimal(str(physical_row.get("balance", 0))),
            ledger_functional_balance=Decimal(str(ledger_row["functional_balance"])),
            ledger_quantity=Decimal(str(ledger_row["quantity"])),
        )

    async def _existing_client_event(
        self, session: AsyncSession, *, actor: ActorContext, request: CashMovementRequest
    ) -> Mapping[str, Any] | None:
        """The movement a replayed ``client_event_id`` already produced, if there is one.

        This is the offline hook (PART 34): a device that recorded a receipt while disconnected
        re-sends it with the same event id, and the server answers with the movement it already
        holds instead of moving the money twice. An event id already attached to a *different*
        currency or amount is refused: the same name cannot mean two different cash acts.
        """
        if request.client_event_id is None:
            return None
        existing = await CashMovementRepository(session).by_client_event(request.client_event_id)
        if existing is None:
            return None
        same_money = _uuid(existing["currency_id"]) == request.currency_id and Decimal(
            str(existing["amount"])
        ) == Decimal(request.amount)
        if not same_money or not self._accounting.branch_in_scope(actor, existing["branch_id"]):
            raise ConflictError(
                "That client event already recorded a different cash movement.",
                details={
                    "reason": "DUPLICATE_RESOURCE",
                    "client_event_id": str(request.client_event_id),
                    "movement_id": str(existing["id"]),
                    "recorded_amount": format_decimal(Decimal(str(existing["amount"]))),
                    "requested_amount": format_decimal(Decimal(request.amount)),
                },
            )
        return existing

    async def _movement_view(
        self, session: AsyncSession, movement_id: uuid.UUID
    ) -> CashMovementView:
        row = await CashMovementRepository(session).row(movement_id)
        if row is None:
            raise ResourceNotFoundError(
                "That cash movement does not exist.",
                details={"resource": "cash_movement", "id": str(movement_id)},
            )
        return CashMovementView(row=row, reversal_movement_id=await self._reversal_id(session, row))

    async def _reversal_id(self, session: AsyncSession, row: Mapping[str, Any]) -> uuid.UUID | None:
        """The movement that reversed ``row``, when there is one (``None`` for a reversal)."""
        if str(row["reference_type"]) == REFERENCE_TYPE_REVERSAL:
            return None
        reversal = await CashMovementRepository(session).reversal_of(_uuid(row["id"]))
        return _uuid(reversal["id"]) if reversal is not None else None

    async def _load_session(
        self, session: AsyncSession, session_id: uuid.UUID, *, with_movements: bool
    ) -> CashSessionView:
        rows = CashSessionRepository(session)
        row = await rows.row(session_id)
        if row is None:
            raise ResourceNotFoundError(
                "That cash session does not exist.",
                details={"resource": "cash_session", "id": str(session_id)},
            )
        movements: Sequence[Mapping[str, Any]] = (
            await CashMovementRepository(session).session_movements(session_id)
            if with_movements
            else ()
        )
        adjustments = await self._adjustment_entries(session, [session_id])
        return self._session_view(
            row,
            lines=await rows.lines(session_id),
            movements=movements,
            adjustment_entry_ids=adjustments.get(session_id, {}),
            with_movements=with_movements,
        )

    def _session_view(
        self,
        row: Mapping[str, Any],
        *,
        lines: Sequence[Mapping[str, Any]],
        adjustment_entry_ids: Mapping[uuid.UUID, uuid.UUID],
        movements: Sequence[Mapping[str, Any]] = (),
        with_movements: bool = False,
    ) -> CashSessionView:
        return CashSessionView(
            row=row,
            lines=tuple(lines),
            movements=(
                tuple(CashMovementView(row=movement) for movement in movements)
                if with_movements
                else ()
            ),
            adjustment_entry_ids=adjustment_entry_ids,
        )

    async def _adjustment_entries(
        self, session: AsyncSession, session_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, dict[uuid.UUID, uuid.UUID]]:
        """Per session and currency: the journal entry of the variance it posted, if any.

        Read from the movements rather than from a column: the frozen schema stores the
        evidence (the movement and its entry) and the contract only promises the reference in
        the payload (``adjustment_journal_entry_id``), so no schema change is needed.
        """
        if not session_ids:
            return {}
        rows = await CashMovementRepository(session).adjustment_entries(session_ids)
        grouped: dict[uuid.UUID, dict[uuid.UUID, uuid.UUID]] = {}
        for row in rows:
            if row["journal_entry_id"] is None:
                continue
            grouped.setdefault(_uuid(row["cash_session_id"]), {})[_uuid(row["currency_id"])] = (
                _uuid(row["journal_entry_id"])
            )
        return grouped

    async def _authorize(
        self, actor: ActorContext, operation: str, *, context: Mapping[str, Any]
    ) -> None:
        """The service-level permission check, with the refusal written to the audit trail.

        The route already denies callers without the permission (PART 41); this is the second
        lock on the same door and the one that leaves a trace, because the service is callable
        from a worker where no route checked anything.
        """
        await self._require_permission(
            actor, OPERATION_PERMISSIONS[operation], operation=operation, context=context
        )

    async def _require_permission(
        self,
        actor: ActorContext,
        permission: Permission,
        *,
        operation: str,
        context: Mapping[str, Any],
    ) -> None:
        """A permission a *step* of an operation needs (a variance is not a plain close)."""
        if str(permission) in actor.permissions:
            return
        await self._record_denial(
            actor, permission=permission, operation=operation, context=context
        )
        raise PermissionDeniedError(
            f"You do not have permission to perform cash.{operation}.",
            details={
                "required_permission": str(permission),
                "operation": operation,
                **dict(context),
            },
        )

    async def _record_denial(
        self,
        actor: ActorContext,
        *,
        permission: Permission,
        operation: str,
        context: Mapping[str, Any],
    ) -> None:
        """Write the refusal in its own transaction, so it survives this request's rollback."""
        async with self._database.transaction() as session:
            AuditService(session).record(
                action=AuditAction.CASH_OPERATION_DENIED,
                entity_type="cash_operation",
                entity_id=None,
                new_data={
                    "reason": "PERMISSION_DENIED",
                    "operation": operation,
                    "required_permission": str(permission),
                    "roles": list(actor.roles),
                    **dict(context),
                },
                actor=actor,
            )

    async def _claim(
        self,
        session: AsyncSession,
        *,
        actor: ActorContext,
        key: uuid.UUID | None,
        endpoint: str,
        fingerprint: Mapping[str, Any],
    ) -> IdempotencyGuard | _NoGuard | CashResult:
        """Take the ``Idempotency-Key``, or return the answer it already holds (PART 40).

        The endpoints the contract forces to carry a key always pass one; the others pass
        ``None`` and get a guard that records nothing, so the call sites stay uniform.
        """
        if key is None:
            return _NoGuard()
        if actor.user_id is None:  # pragma: no cover - a route always names its user
            raise PermissionDeniedError(
                "A cash operation must name the user who performed it.",
                details={"reason": "ACTOR_REQUIRED"},
            )
        guard = IdempotencyGuard(
            session,
            IdempotencyRequest(
                key=key,
                user_id=actor.user_id,
                endpoint=endpoint,
                request_hash=canonical_request_hash(dict(fingerprint)),
                device_id=actor.device_id,
            ),
        )
        replay = await guard.claim()
        if replay is None:
            return guard
        recorded = dict(replay.body)
        return CashResult(
            payload=recorded,
            status_code=replay.status_code,
            replayed=True,
            session_id=_optional_uuid(recorded.get("session_id")),
            movement_id=_optional_uuid(recorded.get("id")),
        )

    def _complete(
        self,
        guard: IdempotencyGuard | _NoGuard,
        *,
        status_code: int,
        payload: Mapping[str, Any],
        resource_id: uuid.UUID,
    ) -> None:
        """Record the answer this key produced, inside the same transaction as the work."""
        if isinstance(guard, _NoGuard):
            return
        guard.complete(
            status_code=status_code,
            body=payload,
            resource_type="cash_operation",
            resource_id=resource_id,
        )


class _NoGuard:
    """A stand-in for the endpoints the contract does not force to carry a key.

    An optional ``Idempotency-Key`` is honoured whenever it is sent (the store is the same
    one); this object keeps the call sites uniform — they all complete exactly once — and the
    only difference is that there is nothing to record.
    """

    __slots__ = ()


def build_cash_service(*, database: Database, settings: Settings) -> CashService:
    """The service the API and the workers construct (PART 63: routes own no logic)."""
    return CashService(database=database, settings=settings)


def money_sum_declared(opening_declared: Decimal, session_net: Decimal) -> Decimal:
    """``opening_declared + session_net``, rounded once at the stored scale.

    A named function because it is the *definition* of a shift's expected amount (§9.4) and
    appears in the close, in its self-check and in the tests: one expression, one meaning.
    """
    return money_difference(opening_declared, -session_net)


def _fingerprint_money(value: Decimal | None) -> str | None:
    """A money value as canonical text for a request fingerprint (never a float)."""
    if value is None:
        return None
    if not isinstance(value, Decimal):  # pragma: no cover - money is Decimal by construction
        raise ValidationError(
            "A money value must be a Decimal.",
            details={"value": repr(value), "reason": "MONEY_MUST_BE_DECIMAL"},
        )
    return format_decimal(value)


def _money(value: Any) -> str:
    return format_decimal(Decimal(str(value)))


def _optional_money(value: Any) -> str | None:
    return None if value is None else _money(value)


def _iso(value: Any) -> Any:
    """A moment as the API renders it: RFC 3339, with ``Z`` for UTC.

    The response models print ``Z`` for a UTC instant, and the idempotency store keeps the
    payload this module produced — so a moment spelled ``+00:00`` here would be recorded in
    a different spelling than the caller received, and a replay served from that record (or
    an auditor comparing the two) would have to normalise by hand. One moment, one
    spelling, decided at the edge.
    """
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None and value.utcoffset() == dt.timedelta(0):
            return f"{value.astimezone(dt.UTC).replace(tzinfo=None).isoformat()}Z"
        return value.isoformat()
    return value


def _uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _optional_uuid(value: Any) -> uuid.UUID | None:
    return _uuid(value) if value else None


def _business_date(row: Mapping[str, Any]) -> str:
    """The branch-local calendar day a shift opened on (its ``opened_at`` in its own zone).

    The shift's business date is *derived*, never accepted from a client: the server owns the
    clock (PART 5), and a back-dated shift is not something a cash control system should be
    able to invent. ``ACCOUNTING_MODEL.md`` §11 states the period-lock limitation this leaves
    open — Phase 12 owns it, and this module does not pretend to close it.
    """
    opened_at = row["opened_at"]
    if not isinstance(opened_at, dt.datetime):  # pragma: no cover - timestamptz is a datetime
        return str(opened_at)
    try:
        return (
            opened_at.astimezone(ZoneInfo(str(row.get("branch_timezone") or "UTC")))
            .date()
            .isoformat()
        )
    except (ZoneInfoNotFoundError, ValueError):
        # An unknown zone is a data problem, not a reason to fail a read: UTC is the honest
        # fallback (PART 5 keeps every stored instant in UTC).
        return opened_at.astimezone(dt.UTC).date().isoformat()


__all__ = [
    "CashBalanceRow",
    "CashCount",
    "CashMovementRequest",
    "CashMovementView",
    "CashOpening",
    "CashResult",
    "CashService",
    "CashSessionView",
    "CloseSessionRequest",
    "OpenSessionRequest",
    "build_cash_service",
]
