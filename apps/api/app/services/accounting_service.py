"""The accounting engine — the only writer of the ledger (PART 12, PART 46, PART 49).

``ACCOUNTING_MODEL.md`` is the specification; this module is its implementation, and the
two are read together. What the engine guarantees, and where each guarantee lives:

* **Nothing unbalanced can be posted.** :meth:`AccountingService.validate_balanced_entry`
  proves Σdebit = Σcredit in ``Decimal`` *before* a row is written, and the deferred
  constraint trigger ``ct_journal_lines_balanced_*`` proves it again at COMMIT. A defect
  in the caller is a ``JOURNAL_UNBALANCED`` (500), a defect in the database is ``NEX02``:
  the two can only disagree if one of them is broken, which is what the pair is for.
* **A posted journal is immutable.** There is no update path and no delete path — not in
  this service, not in the repository, not in SQL (``trg_journal_lines_no_update``,
  no ``UPDATE``/``DELETE`` grant for the runtime role). Corrections are reversals.
* **Every posting is attributable.** One audit row (`JOURNAL_POSTED`) per entry, written
  in the same transaction, carrying the lines, the totals and the *rate snapshot* the
  posting used. A refusal before any write is recorded as `LEDGER_POSTING_DENIED` in its
  own transaction, because the request that caused it is about to be rolled back.
* **Duplicate posting is impossible, twice over.** ``ux_journal_entries_one_per_reference``
  allows exactly one entry per business document, and an optional ``Idempotency-Key``
  replays a completed request instead of repeating it (PART 40).
* **The ledger is functional-currency.** ``debit``/``credit`` are functional amounts,
  ``exchange_rate`` is functional units per unit of the line's currency, and
  ``foreign_amount`` (generated) is the physical quantity. Every leg of every posting
  below is built that way, which is why the entries balance exactly rather than nearly.

Rate handling (the Phase 3 debt, resolved by construction)
----------------------------------------------------------

A posted journal does not *reference* a quote: each line **copies** the rate it used into
``journal_lines.exchange_rate``, and the audit row records which quote (id, instant,
branch, source) that rate came from. Editing ``exchange_rates`` afterwards therefore
cannot restate a posted entry — there is nothing to follow. ``PHASE4_REPORT.md`` §6
records the review that established this and the regression test that keeps it true.

Transaction boundaries
----------------------

Every public method here owns exactly one database transaction
(:meth:`app.core.database.Database.transaction`). The posting rules for exchange, cash
and expenses build a plan and hand it to :meth:`_post`, so a business document (written
by the phase that owns it) and its journal entry commit together — the PART 20 rule the
later phases depend on.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, NoReturn, cast

from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_actions import AuditAction
from app.core.config import Settings
from app.core.database import Database
from app.core.exceptions import (
    KNOWN_SQLSTATES,
    AlreadyReversedError,
    CashCounterAccountRequiredError,
    CurrencyInactiveError,
    DataIntegrityError,
    DuplicateResourceError,
    ExchangeDirectionError,
    ForbiddenScopeError,
    InsufficientBalanceError,
    JournalUnbalancedError,
    NexusError,
    PermissionDeniedError,
    RateNotFoundError,
    ResourceNotFoundError,
    ReversalError,
    ValidationError,
    constraint_name_of,
    error_for_sqlstate,
    sqlstate_of,
)
from app.core.idempotency import (
    ENDPOINT_LEDGER_POSTING,
    IdempotencyGuard,
    IdempotencyRequest,
    canonical_request_hash,
)
from app.core.logging import get_logger
from app.core.money import (
    MoneyError,
    assert_within_money_bounds,
    divide_money,
    format_decimal,
    has_money_scale,
    money_difference,
    money_sum,
    multiply_money,
    quantize_money,
)
from app.core.permissions import GROUP_WIDE_ROLES, Permission
from app.models.account import Account
from app.models.cash import CashMovement
from app.models.currency import Currency
from app.models.journal import JournalEntry, JournalLine
from app.repositories.ledger import JournalEntryRepository, LedgerRepository
from app.repositories.ledger_master import AccountRepository, ExchangeRateRepository
from app.repositories.masterdata import BranchRepository, CurrencyRepository
from app.services.audit_service import ActorContext, AuditService

# --------------------------------------------------------------------------- policy

# Which authority a posting requires, by the type of document it belongs to. The map is
# the *whole* authorization rule for the ledger: no code path posts without consulting
# it, so a phase that adds a document type must add its permission here — a compile-time
# visible decision instead of a missing check.
POSTING_AUTHORITY: dict[str, Permission] = {
    "EXCHANGE_TRANSACTION": Permission.EXCHANGE_CREATE,
    "CASH_MOVEMENT": Permission.CASH_CREATE,
    "EXPENSE": Permission.EXPENSES_CREATE,
    "TRANSFER": Permission.TRANSFERS_CREATE,
    "MANUAL_ADJUSTMENT": Permission.ACCOUNTS_MANAGE,
    "OPENING_BALANCE": Permission.ACCOUNTS_MANAGE,
}

# Undoing a document is a different authority from creating it (API_CONTRACT §8):
# reversing an exchange transaction is `exchange.reverse`, cancelling an expense is
# `expenses.create`, undoing a cash movement is `cash.adjust` (the permission that exists
# for money corrections), cancelling a transfer is `transfers.cancel`.
REVERSAL_AUTHORITY: dict[str, Permission] = {
    "EXCHANGE_TRANSACTION": Permission.EXCHANGE_REVERSE,
    "CASH_MOVEMENT": Permission.CASH_ADJUST,
    "EXPENSE": Permission.EXPENSES_CREATE,
    "TRANSFER": Permission.TRANSFERS_CANCEL,
    "MANUAL_ADJUSTMENT": Permission.ACCOUNTS_MANAGE,
    "OPENING_BALANCE": Permission.ACCOUNTS_MANAGE,
}

# Some documents have two undo doors, and RBAC gives them different permissions: an
# accountant may cancel an exchange but may not reverse one (``ACCOUNTANT`` holds
# ``exchange.cancel`` without ``exchange.reverse``). The default above is the reversal door;
# a document service that is performing its *cancellation* names that door explicitly, and
# the ledger then checks the permission the caller actually claimed. The set is closed per
# reference type, so a caller can choose between the authorities the contract grants for
# that document and nothing else.
REVERSAL_AUTHORITY_CHOICES: dict[str, frozenset[Permission]] = {
    "EXCHANGE_TRANSACTION": frozenset({Permission.EXCHANGE_REVERSE, Permission.EXCHANGE_CANCEL}),
}

# A single entry may not reference itself (the frozen CHECK also says so).
REFERENCE_TYPES = tuple(POSTING_AUTHORITY)

# Everything except a manual adjustment is the journal of exactly one business document,
# so the document id is mandatory: it is what makes "one entry per document" provable.
REFERENCE_TYPES_REQUIRING_DOCUMENT = tuple(
    reference_type for reference_type in REFERENCE_TYPES if reference_type != "MANUAL_ADJUSTMENT"
)

# Branch scope: which roles act group-wide instead of branch-bound. The policy lives in
# ``app.core.permissions`` (it is a property of the role catalogue, not of the ledger), and
# the ledger reads it from there so a future permission-based delegation changes one place.

CASH_MOVEMENT_TYPES = ("IN", "OUT", "ADJUSTMENT", "OPENING")
EXCHANGE_TRANSACTION_TYPES = ("BUY", "SELL")

# Why these two are refused as reversed *targets*: a reversal already restores the state
# the original entry changed (its net effect on every account is zero), so reversing it
# again would be a re-posting of the original document under a different name. The
# correct move is a new document, or reversing the original document's own reversal
# request — which is what the operator actually means.
NON_REVERSIBLE_REFERENCE_TYPES = ("REVERSAL",)


# ----------------------------------------------------------------------- value types
@dataclass(frozen=True, slots=True)
class PostingLine:
    """One side of a journal entry, in functional-currency terms.

    ``debit``/``credit`` are functional amounts; ``currency_id`` is the currency the
    account holds and ``exchange_rate`` is functional units per one unit of it (1 for the
    functional currency itself). ``foreign_amount`` is derived by the database from those
    three, so a Python-side copy of it could never disagree with the stored column.
    """

    account_id: uuid.UUID
    currency_id: uuid.UUID
    debit: Decimal = Decimal(0)
    credit: Decimal = Decimal(0)
    exchange_rate: Decimal = Decimal(1)
    description: str | None = None


@dataclass(frozen=True, slots=True)
class PostingTotals:
    """The sums a balanced entry must satisfy."""

    lines: int
    debit: Decimal
    credit: Decimal

    @property
    def difference(self) -> Decimal:
        return money_difference(self.debit, self.credit)

    @property
    def is_balanced(self) -> bool:
        return self.difference == 0


@dataclass(frozen=True, slots=True)
class ExchangeComputation:
    """The arithmetic of one exchange deal, computed once for both doors.

    ``gross_amount`` is ``from_amount x exchange_rate`` expressed in the deal's *to*
    currency, ``settlement_amount`` is what actually moves on that side (the payout after
    the commission for a BUY, the full receipt for a SELL — ``ACCOUNTING_MODEL.md`` §6.2,
    §6.3). The document service stores the same numbers the ledger posts, because both
    call :func:`compute_exchange_amounts`.
    """

    transaction_type: str
    from_amount: Decimal
    exchange_rate: Decimal
    commission: Decimal
    gross_amount: Decimal
    settlement_amount: Decimal

    def to_payload(self) -> dict[str, Any]:
        return {
            "transaction_type": self.transaction_type,
            "from_amount": format_decimal(self.from_amount),
            "exchange_rate": format_decimal(self.exchange_rate),
            "commission": format_decimal(self.commission),
            "gross_amount": format_decimal(self.gross_amount),
            "settlement_amount": format_decimal(self.settlement_amount),
        }


@dataclass(frozen=True, slots=True)
class CashMovementSpec:
    """One physical cash movement a business document produces.

    ``amount`` is a **quantity** of ``currency_id`` (never a functional value): it is what a
    cashier counts, and it is what the ``ct_cash_movements_non_negative`` constraint sums
    (``ACCOUNTING_MODEL.md`` §2, §6).
    """

    account_id: uuid.UUID
    currency_id: uuid.UUID
    movement_type: str
    amount: Decimal
    description: str | None = None
    adjustment_sign: int | None = None


@dataclass(frozen=True, slots=True)
class RateSnapshot:
    """The quote a posting used, kept for provenance (never re-resolved later).

    The journal line stores the *number*; this records *where the number came from*, so an
    auditor can see which quote a counter applied even after newer quotes exist.
    """

    rate: Decimal
    rate_id: uuid.UUID | None = None
    from_currency_id: uuid.UUID | None = None
    to_currency_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    effective_at: dt.datetime | None = None
    source: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "rate": format_decimal(self.rate),
            "exchange_rate_id": str(self.rate_id) if self.rate_id else None,
            "from_currency_id": str(self.from_currency_id) if self.from_currency_id else None,
            "to_currency_id": str(self.to_currency_id) if self.to_currency_id else None,
            "branch_id": str(self.branch_id) if self.branch_id else None,
            "effective_at": self.effective_at.isoformat() if self.effective_at else None,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class JournalLineView:
    """One line as it is read back, with the account and currency facts clients need."""

    id: uuid.UUID
    account_id: uuid.UUID
    account_code: str
    account_name: str
    account_type: str
    currency_id: uuid.UUID
    currency_code: str | None
    debit: Decimal
    credit: Decimal
    exchange_rate: Decimal
    foreign_amount: Decimal | None
    description: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "account_id": str(self.account_id),
            "account_code": self.account_code,
            "account_name": self.account_name,
            "account_type": self.account_type,
            "currency_id": str(self.currency_id),
            "currency_code": self.currency_code,
            "debit": format_decimal(self.debit),
            "credit": format_decimal(self.credit),
            "exchange_rate": format_decimal(self.exchange_rate),
            "foreign_amount": None
            if self.foreign_amount is None
            else format_decimal(self.foreign_amount),
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class JournalEntryView:
    """A posted journal entry: header, totals, reversal linkage and its lines."""

    id: uuid.UUID
    reference_type: str
    reference_id: uuid.UUID | None
    description: str | None
    transaction_date: dt.datetime
    created_at: dt.datetime
    created_by: uuid.UUID | None
    created_by_username: str | None
    branch_id: uuid.UUID | None
    branch_code: str | None
    device_id: uuid.UUID | None
    reversal_of_id: uuid.UUID | None
    reversed_by_entry_id: uuid.UUID | None
    line_count: int
    total_debit: Decimal
    total_credit: Decimal
    lines: tuple[JournalLineView, ...] = ()

    @property
    def is_balanced(self) -> bool:
        return self.total_debit == self.total_credit

    def to_payload(self, *, with_lines: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": str(self.id),
            "reference_type": self.reference_type,
            "reference_id": str(self.reference_id) if self.reference_id else None,
            "description": self.description,
            "transaction_date": self.transaction_date.isoformat(),
            "created_at": self.created_at.isoformat(),
            "created_by": str(self.created_by) if self.created_by else None,
            "created_by_username": self.created_by_username,
            "branch_id": str(self.branch_id) if self.branch_id else None,
            "branch_code": self.branch_code,
            "device_id": str(self.device_id) if self.device_id else None,
            "reversal_of_id": str(self.reversal_of_id) if self.reversal_of_id else None,
            "reversed_by_entry_id": str(self.reversed_by_entry_id)
            if self.reversed_by_entry_id
            else None,
            "line_count": self.line_count,
            "total_debit": format_decimal(self.total_debit),
            "total_credit": format_decimal(self.total_credit),
            "is_balanced": self.is_balanced,
        }
        if with_lines:
            payload["lines"] = [line.to_payload() for line in self.lines]
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> JournalEntryView:
        """Rebuild a view from its own payload (used to replay a stored answer).

        The round trip is exact for every field, which is what makes an idempotent replay
        byte-identical to the original response — asserted by a unit test rather than
        assumed.
        """
        return cls(
            id=uuid.UUID(str(payload["id"])),
            reference_type=str(payload["reference_type"]),
            reference_id=_optional_uuid(payload.get("reference_id")),
            description=_optional_str(payload.get("description")),
            transaction_date=dt.datetime.fromisoformat(str(payload["transaction_date"])),
            created_at=dt.datetime.fromisoformat(str(payload["created_at"])),
            created_by=_optional_uuid(payload.get("created_by")),
            created_by_username=_optional_str(payload.get("created_by_username")),
            branch_id=_optional_uuid(payload.get("branch_id")),
            branch_code=_optional_str(payload.get("branch_code")),
            device_id=_optional_uuid(payload.get("device_id")),
            reversal_of_id=_optional_uuid(payload.get("reversal_of_id")),
            reversed_by_entry_id=_optional_uuid(payload.get("reversed_by_entry_id")),
            line_count=int(payload.get("line_count") or 0),
            total_debit=Decimal(str(payload["total_debit"])),
            total_credit=Decimal(str(payload["total_credit"])),
            lines=tuple(
                JournalLineView(
                    id=uuid.UUID(str(line["id"])),
                    account_id=uuid.UUID(str(line["account_id"])),
                    account_code=str(line["account_code"]),
                    account_name=str(line["account_name"]),
                    account_type=str(line["account_type"]),
                    currency_id=uuid.UUID(str(line["currency_id"])),
                    currency_code=_optional_str(line.get("currency_code")),
                    debit=Decimal(str(line["debit"])),
                    credit=Decimal(str(line["credit"])),
                    exchange_rate=Decimal(str(line["exchange_rate"])),
                    foreign_amount=None
                    if line.get("foreign_amount") is None
                    else Decimal(str(line["foreign_amount"])),
                    description=_optional_str(line.get("description")),
                )
                for line in payload.get("lines") or ()
            ),
        )


@dataclass(frozen=True, slots=True)
class AccountBalanceRow:
    """One (account, currency) balance derived from the immutable lines."""

    currency_id: uuid.UUID | None
    currency_code: str | None
    debit_total: Decimal
    credit_total: Decimal
    balance: Decimal
    entry_count: int
    last_posted_at: dt.datetime | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "currency_id": str(self.currency_id) if self.currency_id else None,
            "currency_code": self.currency_code,
            "debit_total": format_decimal(self.debit_total),
            "credit_total": format_decimal(self.credit_total),
            "balance": format_decimal(self.balance),
            "entry_count": self.entry_count,
            "last_posted_at": self.last_posted_at.isoformat() if self.last_posted_at else None,
        }


@dataclass(frozen=True, slots=True)
class AccountBalanceView:
    """An account's balances, per currency, with the sign convention of its type."""

    account_id: uuid.UUID
    account_code: str
    account_name: str
    account_type: str
    normal_balance: str | None
    is_active: bool
    branch_ids: tuple[uuid.UUID, ...] | None
    as_of: dt.datetime | None
    rows: tuple[AccountBalanceRow, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "account_id": str(self.account_id),
            "account_code": self.account_code,
            "account_name": self.account_name,
            "account_type": self.account_type,
            "normal_balance": self.normal_balance,
            "is_active": self.is_active,
            "branch_ids": [str(branch) for branch in self.branch_ids]
            if self.branch_ids is not None
            else None,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "rows": [row.to_payload() for row in self.rows],
        }


@dataclass(frozen=True, slots=True)
class TrialBalanceRow:
    """One (account, currency) row of the trial balance.

    ``net_debit`` is the frozen ``v_trial_balance`` column of that name
    (``SUM(debit) - SUM(credit)``); ``balance`` is the same figure on the account's normal side,
    which is what the ``v_account_balances`` convention reports.
    """

    account_id: uuid.UUID
    account_code: str
    account_name: str
    account_type: str
    normal_balance: str | None
    currency_id: uuid.UUID | None
    currency_code: str | None
    total_debit: Decimal
    total_credit: Decimal
    net_debit: Decimal
    balance: Decimal
    entry_count: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "account_id": str(self.account_id),
            "account_code": self.account_code,
            "account_name": self.account_name,
            "account_type": self.account_type,
            "normal_balance": self.normal_balance,
            "currency_id": str(self.currency_id) if self.currency_id else None,
            "currency_code": self.currency_code,
            "total_debit": format_decimal(self.total_debit),
            "total_credit": format_decimal(self.total_credit),
            "net_debit": format_decimal(self.net_debit),
            "balance": format_decimal(self.balance),
            "entry_count": self.entry_count,
        }


@dataclass(frozen=True, slots=True)
class TrialBalanceView:
    """The trial balance with its own proof: Σdebit must equal Σcredit.

    ``source`` names the immutable table the report was derived from and ``filters``
    repeats the filters it was generated with, so a printed report can be reproduced
    exactly (API_CONTRACT §9).
    """

    rows: tuple[TrialBalanceRow, ...]
    total_debit: Decimal
    total_credit: Decimal
    generated_at: dt.datetime
    branch_id: uuid.UUID | None
    account_type: str | None = None
    from_date: dt.datetime | None = None
    to_date: dt.datetime | None = None
    include_inactive: bool = True

    @property
    def is_balanced(self) -> bool:
        return self.total_debit == self.total_credit

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": "journal_lines",
            "generated_at": self.generated_at.isoformat(),
            "filters": {
                "branch_id": str(self.branch_id) if self.branch_id else None,
                "account_type": self.account_type,
                "from": self.from_date.isoformat() if self.from_date else None,
                "to": self.to_date.isoformat() if self.to_date else None,
                "include_inactive": self.include_inactive,
            },
            "total_debit": format_decimal(self.total_debit),
            "total_credit": format_decimal(self.total_credit),
            "difference": format_decimal(self.total_debit - self.total_credit),
            "is_balanced": self.is_balanced,
            "rows": [row.to_payload() for row in self.rows],
        }


@dataclass(frozen=True, slots=True)
class _PostingPlan:
    """Everything one posting needs, validated and ready to write.

    ``fingerprint`` is what an ``Idempotency-Key`` is bound to: the caller's *inputs*, not
    the resolved plan. A retry that omits ``transaction_date`` or ``device_id`` is the
    same request and must replay; the server clock and the device are not part of what the
    client asked for.
    """

    reference_type: str
    reference_id: uuid.UUID | None
    branch_id: uuid.UUID | None
    lines: tuple[PostingLine, ...]
    fingerprint: Mapping[str, Any]
    description: str | None = None
    transaction_date: dt.datetime = field(default_factory=lambda: dt.datetime.now(tz=dt.UTC))
    device_id: uuid.UUID | None = None
    rate_snapshot: RateSnapshot | None = None
    # Set by the *generic* door (``create_journal_entry``) and by nothing else: a manual
    # journal states its own rates and is not backed by a business document, so it is the
    # one path where the ledger has to police its own physical positions (§6.3's guard,
    # invariant I-5). Document paths keep their own rules and write the ``cash_movements``
    # row whose ``NEX01`` constraint is the physical authority for them.
    guard_inventory: bool = False


def _functional_balance(normal_balance: str | None, debit: Decimal, credit: Decimal) -> Decimal:
    """Signed balance using the account's normal side (``v_account_balances`` convention).

    ``normal_balance`` arrives from raw SQL as ``CHAR(6)``, which PostgreSQL blank-pads —
    the Phase 3 defect that leaked ``"DEBIT "`` into responses. Stripping here is the
    reason this helper exists instead of an inline comparison.
    """
    normal = (normal_balance or "").strip().upper()
    return credit - debit if normal == "CREDIT" else debit - credit


def _optional_uuid(value: Any) -> uuid.UUID | None:
    return uuid.UUID(str(value)) if value else None


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


class AccountingService:
    """The single writer of ``journal_entries``/``journal_lines`` (PART 46)."""

    def __init__(self, *, database: Database, settings: Settings) -> None:
        self._database = database
        self._settings = settings

    # ============================================================== transaction
    @asynccontextmanager
    async def _ledger_transaction(
        self, session: AsyncSession | None = None
    ) -> AsyncIterator[AsyncSession]:
        """One transaction per posting, with the database's refusal named.

        When ``session`` is given, the *caller* owns the boundary and nothing is committed
        here. That is what lets a later phase write a business document, its journal entry,
        its cash movement and its audit rows in one transaction (PART 20): the phase opens
        the transaction, calls the ledger inside it, and commits once. The engine still
        validates and still translates the database's refusals; it simply does not decide
        when the work becomes visible.

        The engine validates before it writes, so the only way a constraint can fire is
        (a) a caller that bypassed the service, (b) two transactions racing, or (c) a
        defect here. In all three cases the caller must receive the *documented* domain
        error rather than a raw driver exception, and the mapping is the same one the API
        error handlers use — one table, so the service and the HTTP layer cannot drift.

        Only SQLSTATEs the domain already knows are translated. Anything else (a dropped
        connection, a permission problem, a real bug) propagates untouched: inventing a
        financial meaning for an unknown database failure would be worse than surfacing
        it.
        """
        try:
            if session is not None:
                yield session
            else:
                async with self._database.transaction() as owned:
                    yield owned
        except IntegrityError as exc:
            refusal = _ledger_refusal(exc)
            if refusal is None:
                raise
            logger.warning(
                "ledger_posting_refused",
                sqlstate=sqlstate_of(exc),
                constraint=constraint_name_of(exc),
                reason=str(refusal.code),
            )
            raise refusal from exc

    # ================================================== document-service interface
    # Everything below exists so a document service (Phase 5's exchange engine, Phase 6's
    # cash module) can orchestrate a business document **through** the ledger instead of
    # beside it: one transaction boundary, one scope rule, one writer of financial and
    # physical rows. Nothing here is a second posting path — `_post` remains the only
    # place an entry is inserted.
    def document_transaction(self) -> AbstractAsyncContextManager[AsyncSession]:
        """The ledger's posting transaction, for the service that owns a document.

        Same transaction manager, same translation of the database's refusals, but the
        *caller* owns the boundary: a document row, its journal entry, its cash movements
        and its audit rows become visible together (PART 20) or not at all.
        """
        return self._ledger_transaction()

    async def assert_branch_scope(
        self,
        actor: ActorContext,
        *,
        branch_id: uuid.UUID | None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        """Public form of the posting scope guard (one implementation, one audit trail)."""
        await self._assert_scope(actor, branch_id=branch_id, context=context)

    async def branch_scope_filter(
        self, actor: ActorContext, *, branch_id: uuid.UUID | None = None
    ) -> list[uuid.UUID] | None:
        """Public form of the read scope (``None`` = every branch, ``[]`` = none)."""
        return await self._read_scope(actor, branch_id=branch_id)

    def branch_in_scope(self, actor: ActorContext, branch_id: uuid.UUID | None) -> bool:
        """Whether one row's branch is inside the actor's scope (point reads/writes)."""
        return self._can_see_branch(actor, branch_id)

    def accounting_date(self, value: dt.datetime | None) -> dt.datetime:
        """The document's accounting date: UTC, back-dating allowed, the future refused.

        The rule is the ledger's own (:func:`_accounting_date`), exposed so a document
        service dates its row and its entry identically — a document stamped "tomorrow"
        beside an entry stamped "today" is the kind of drift an auditor finds years later.
        """
        return _accounting_date(value, self._settings)

    async def resolve_exchange_currencies(
        self,
        session: AsyncSession,
        *,
        transaction_type: str,
        from_currency_id: uuid.UUID,
        to_currency_id: uuid.UUID,
    ) -> tuple[Currency, Currency, Currency]:
        """The two sides of a deal and the functional currency, with the direction checked.

        Returns ``(from_currency, to_currency, functional_currency)``. The refusals are the
        ledger's own and happen *here*, before the document service looks up a rate: a
        missing currency (404), an inactive one (422 ``CURRENCY_INACTIVE``), the same
        currency on both sides or the functional currency on the delivered side
        (422 ``EXCHANGE_DIRECTION_INVALID``). A deal that cannot exist therefore never
        reaches rate resolution, amount computation or posting (Phase 5 §5), and the rule
        stays single-sourced in ``_assert_exchange_direction`` rather than being restated
        by every caller that wants to validate a deal early.
        """
        currency_type = _transaction_type(transaction_type)
        currencies = CurrencyRepository(session)
        from_currency = await _require_currency(currencies, from_currency_id, "from_currency_id")
        to_currency = await _require_currency(currencies, to_currency_id, "to_currency_id")
        base = await _require_base_currency(session)
        _assert_exchange_direction(
            transaction_type=currency_type,
            from_currency=from_currency,
            to_currency=to_currency,
            base=base,
        )
        return from_currency, to_currency, base

    async def inventory_account(
        self, session: AsyncSession, *, branch_id: uuid.UUID, currency_id: uuid.UUID
    ) -> Account:
        """The branch's drawer for one currency: its **inventory account** (§2).

        An inventory account is an active, postable ``ASSET`` account bound to the currency
        — the only kind of account that holds a position. Which of the chart's asset
        accounts *is* the drawer is answered in a fixed order, because a branch must always
        trade out of the same till:

        1. the branch's **own** account for the currency (``accounts.branch_id`` — §10: a
           branch can never spend another branch's cash);
        2. failing that, the group-wide account in the **inventory band** the accounting
           model reserves for cash (``1000`` to ``1099``, §5);
        3. failing that, the single group-wide asset account bound to the currency.

        A step that finds more than one candidate refuses (``AMBIGUOUS_CASH_ACCOUNT``)
        rather than choosing: silently posting a branch's cash into one of two drawers
        would make the till disagree with the ledger, and the chart is a one-minute fix.
        The refusal names the currency, the branch and the candidate codes.
        """
        candidates = [
            row
            for row in (
                (
                    await session.execute(
                        select(Account)
                        .where(
                            Account.account_type == "ASSET",
                            Account.currency_id == currency_id,
                            Account.is_active.is_(True),
                            Account.is_postable.is_(True),
                        )
                        .order_by(Account.code)
                    )
                )
                .scalars()
                .all()
            )
            if row.branch_id in (None, branch_id)
        ]
        if not candidates:
            raise ValidationError(
                "This branch has no cash account for that currency, so the deal cannot move cash.",
                details={
                    "fields": [{"field": "currency_id", "code": "no_cash_account"}],
                    "branch_id": str(branch_id),
                    "currency_id": str(currency_id),
                    "hint": (
                        "Create an active, postable ASSET account bound to this currency, "
                        "group-wide or for this branch."
                    ),
                },
            )
        # Step 1 then step 2, in one expression: the branch's own drawer first, then the
        # group-wide account in the cash band. Step 3 is the fallback below.
        chosen_pool: list[Account] = [row for row in candidates if row.branch_id == branch_id] or [
            row for row in candidates if _in_inventory_band(row.code)
        ]
        chosen_pool = chosen_pool or candidates
        if len(chosen_pool) > 1:
            raise DataIntegrityError(
                "More than one cash account can hold this currency at this branch.",
                details={
                    "reason": "AMBIGUOUS_CASH_ACCOUNT",
                    "branch_id": str(branch_id),
                    "currency_id": str(currency_id),
                    "candidate_codes": [row.code for row in chosen_pool],
                    "hint": (
                        "Keep one inventory account per currency per branch: bind the "
                        "branch's own account, or leave a single account in the "
                        "1000-1099 cash band."
                    ),
                },
            )
        return chosen_pool[0]

    async def record_cash_movements(
        self,
        session: AsyncSession,
        *,
        reference_type: str,
        reference_id: uuid.UUID,
        branch_id: uuid.UUID,
        movements: Sequence[CashMovementSpec],
        actor: ActorContext,
        journal_entry_id: uuid.UUID | None = None,
        cash_session_id: uuid.UUID | None = None,
        device_id: uuid.UUID | None = None,
        client_event_id: uuid.UUID | None = None,
        allow_inactive_branch: bool = False,
    ) -> list[uuid.UUID]:
        """Write the physical side of a business document (``cash_movements``).

        The ledger records what the books say; this table records what the drawer holds, and
        the two are reconciled through the signed amount (``ACCOUNTING_MODEL.md`` §2, §8).
        A document service must write both, in one transaction, or the reconciliation stops
        meaning anything — which is why the write lives here, next to the entry it belongs
        to, instead of in each document service.

        What this method does **not** do is decide whether a position is sufficient:
        ``ct_cash_movements_non_negative`` (``NEX01``) is the authority and is deferred to
        COMMIT precisely so a document may write both legs of a movement before anyone
        judges the total.

        The ledger's own disposal guard (``_carrying_rate``) has already read the position
        under the account lock by the time a document gets here, so a shortfall is normally
        refused with its domain error and ``NEX01`` remains the database's independent
        backstop.

        ``allow_inactive_branch`` exists for the same reason the ledger exempts reversals
        from its "no postings at a closed branch" rule: retiring a branch must not make its
        history uncorrectable, and a correction at a closed branch is still a correction.
        """
        if actor.user_id is None:
            raise PermissionDeniedError(
                "A cash movement must name the user who recorded it.",
                details={"reason": "ACTOR_REQUIRED"},
            )
        if not movements:
            raise ValidationError(
                "A document that moves cash must state at least one movement.",
                details={"reason": "NO_MOVEMENTS"},
            )
        branch = await BranchRepository(session).get(branch_id)
        if branch is None:
            raise ResourceNotFoundError(
                "That branch does not exist.",
                details={"fields": [{"field": "branch_id", "code": "not_found"}]},
            )
        if not branch.is_active and reference_type != "REVERSAL" and not allow_inactive_branch:
            raise ValidationError(
                "A closed branch cannot record new cash movements.",
                details={"fields": [{"field": "branch_id", "code": "inactive"}]},
            )

        accounts: dict[uuid.UUID, Account] = {}
        currencies: dict[uuid.UUID, Currency] = {}
        rows: list[uuid.UUID] = []
        for index, movement in enumerate(movements):
            if movement.movement_type not in (*CASH_MOVEMENT_TYPES, "EXPENSE", "CLOSING"):
                raise ValidationError(
                    "That is not a cash movement type.",
                    details={
                        "fields": [
                            {"field": f"movements[{index}].movement_type", "code": "unsupported"}
                        ],
                        "movement_type": movement.movement_type,
                    },
                )
            account = accounts.get(movement.account_id)
            if account is None:
                loaded = await AccountRepository(session).get(movement.account_id)
                if loaded is not None:
                    accounts[movement.account_id] = loaded
                account = loaded
            if account is None:
                raise ResourceNotFoundError(
                    "That account does not exist.",
                    details={
                        "fields": [{"field": f"movements[{index}].account_id", "code": "not_found"}]
                    },
                )
            if not account.is_active or not account.is_postable:
                raise ValidationError(
                    "A cash movement needs an active, postable account.",
                    details={
                        "fields": [
                            {"field": f"movements[{index}].account_id", "code": "not_postable"}
                        ]
                    },
                )
            if account.currency_id is not None and account.currency_id != movement.currency_id:
                raise ValidationError(
                    "The movement's currency is not the account's currency.",
                    details={
                        "fields": [
                            {
                                "field": f"movements[{index}].currency_id",
                                "code": "currency_mismatch",
                            }
                        ],
                        "account_currency_id": str(account.currency_id),
                    },
                )
            if account.branch_id is not None and account.branch_id != branch_id:
                raise ValidationError(
                    "That account belongs to another branch.",
                    details={
                        "fields": [
                            {"field": f"movements[{index}].account_id", "code": "branch_mismatch"}
                        ],
                        "account_branch_id": str(account.branch_id),
                    },
                )
            currency = currencies.get(movement.currency_id)
            if currency is None:
                loaded_currency = await CurrencyRepository(session).get(movement.currency_id)
                if loaded_currency is not None:
                    currencies[movement.currency_id] = loaded_currency
                currency = loaded_currency
            if currency is None:
                raise ResourceNotFoundError(
                    "That currency does not exist.",
                    details={
                        "fields": [
                            {"field": f"movements[{index}].currency_id", "code": "not_found"}
                        ]
                    },
                )
            amount = _non_negative(movement.amount, field=f"movements[{index}].amount")
            if amount != quantize_money(amount, currency.decimal_places):
                raise ValidationError(
                    "The movement cannot be expressed in the currency's smallest unit.",
                    details={
                        "fields": [
                            {
                                "field": f"movements[{index}].amount",
                                "code": "below_smallest_unit",
                            }
                        ],
                        "currency_code": currency.code,
                        "decimal_places": currency.decimal_places,
                    },
                )
            if movement.movement_type == "ADJUSTMENT" and movement.adjustment_sign not in (-1, 1):
                raise ValidationError(
                    "An adjustment must state its direction.",
                    details={
                        "fields": [
                            {
                                "field": f"movements[{index}].adjustment_sign",
                                "code": "required",
                            }
                        ]
                    },
                )
            if movement.movement_type != "ADJUSTMENT" and movement.adjustment_sign is not None:
                raise ValidationError(
                    "Only an adjustment carries a direction sign.",
                    details={
                        "fields": [
                            {"field": f"movements[{index}].adjustment_sign", "code": "unexpected"}
                        ]
                    },
                )
            row = CashMovement(
                branch_id=branch_id,
                account_id=movement.account_id,
                currency_id=movement.currency_id,
                movement_type=movement.movement_type,
                amount=amount,
                reference_type=reference_type,
                reference_id=reference_id,
                description=movement.description,
                created_by=actor.user_id,
                adjustment_sign=movement.adjustment_sign,
                cash_session_id=cash_session_id,
                device_id=device_id or actor.device_id,
                journal_entry_id=journal_entry_id,
                client_event_id=client_event_id,
            )
            session.add(row)
            rows.append(row.id)
        await session.flush()
        return rows

    # =============================================================== validation
    async def _lock_accounts(self, session: AsyncSession, *account_ids: uuid.UUID | None) -> None:
        """Take the per-account posting lock, before anything is decided from a balance.

        Ordered by id, and taken for every account the entry may touch, so two postings
        that share accounts queue instead of deadlocking and no rule that reads a position
        can be raced by the posting next to it. ``None`` is simply not an account.
        """
        await JournalEntryRepository(session).lock_accounts(
            [account_id for account_id in account_ids if account_id is not None]
        )

    @staticmethod
    def validate_balanced_entry(lines: Sequence[PostingLine]) -> PostingTotals:
        """Prove an entry is postable, in ``Decimal``, before anything is written.

        Rejects — with the exact numbers in the error, because a caller debugging a
        posting plan needs them:

        * fewer than two lines (a one-sided entry is not double entry);
        * a line with both sides positive, or neither (zero-value line);
        * a negative amount (the database refuses it too: ``ck_journal_lines_*``);
        * a non-positive or excessively precise rate;
        * an amount that ``NUMERIC(30,10)`` cannot hold, or that has more than 10
          decimals (silently rounding money is exactly how a ledger stops reconciling);
        * Σdebit ≠ Σcredit.
        """
        if len(lines) < 2:
            raise JournalUnbalancedError(
                "A journal entry needs at least two lines.",
                details={"lines": len(lines), "minimum": 2},
            )

        debits: list[Decimal] = []
        credits: list[Decimal] = []
        for index, line in enumerate(lines):
            debit = _validated_money(line.debit, field=f"lines[{index}].debit")
            credit = _validated_money(line.credit, field=f"lines[{index}].credit")
            rate = _validated_money(line.exchange_rate, field=f"lines[{index}].exchange_rate")

            if debit < 0 or credit < 0:
                raise JournalUnbalancedError(
                    "A journal line may not carry a negative amount.",
                    details={"line": index, "debit": str(debit), "credit": str(credit)},
                )
            if (debit > 0) == (credit > 0):
                raise JournalUnbalancedError(
                    "Each journal line must have exactly one side greater than zero.",
                    details={
                        "line": index,
                        "debit": str(debit),
                        "credit": str(credit),
                        "reason": "DEBIT_AND_CREDIT" if debit > 0 else "ZERO_VALUE",
                    },
                )
            if rate <= 0:
                raise JournalUnbalancedError(
                    "A journal line's exchange rate must be greater than zero.",
                    details={"line": index, "exchange_rate": str(rate)},
                )

            debits.append(debit)
            credits.append(credit)

        # Summed in the wide money context: adding 10-decimal values is exact there, while
        # ``sum()`` under the default 28-digit context silently rounds a large total.
        totals = PostingTotals(lines=len(lines), debit=money_sum(debits), credit=money_sum(credits))
        if not totals.is_balanced:
            raise JournalUnbalancedError(
                "Total debit must equal total credit (PART 49).",
                details={
                    "total_debit": format_decimal(totals.debit),
                    "total_credit": format_decimal(totals.credit),
                    "difference": format_decimal(totals.difference),
                    "lines": totals.lines,
                },
            )
        return totals

    # ============================================================ public posting
    async def create_journal_entry(
        self,
        *,
        reference_type: str,
        reference_id: uuid.UUID | None,
        lines: Sequence[PostingLine],
        actor: ActorContext,
        branch_id: uuid.UUID,
        description: str | None = None,
        transaction_date: dt.datetime | None = None,
        device_id: uuid.UUID | None = None,
        rate_snapshot: RateSnapshot | None = None,
        idempotency_key: uuid.UUID | None = None,
        endpoint: str | None = ENDPOINT_LEDGER_POSTING,
        session: AsyncSession | None = None,
    ) -> JournalEntryView:
        """Post one balanced entry for one business event.

        This is the generic door into the ledger. The posting rules for exchange, cash and
        expenses go through it too, so there is exactly one place where an entry is
        validated, inserted, audited and made idempotent.

        Unlike a *document* posting — which is backed by the ``cash_movements`` row whose
        ``ct_cash_movements_non_negative`` constraint is the physical authority — a manual
        entry has no document behind it, so the ledger polices its own physical positions
        here: no line may deliver more of a currency than the branch's inventory account
        holds (``ACCOUNTING_MODEL.md`` §6.3, invariant I-5). Before the Gate Review
        regression this door had no such guard and a manual credit to an inventory account
        drove its position negative; see §13 of the model and the Gate Review section of
        ``docs/phases/PHASE4_REPORT.md``.
        """
        reference_type = _reference_type(reference_type)
        await self._authorize(reference_type, actor)
        plan = _PostingPlan(
            guard_inventory=True,
            reference_type=reference_type,
            reference_id=reference_id,
            branch_id=branch_id,
            lines=tuple(lines),
            fingerprint=_fingerprint(
                reference_type=reference_type,
                reference_id=reference_id,
                branch_id=branch_id,
                description=description,
                transaction_date=transaction_date,
                lines=_fingerprint_lines(lines),
            ),
            description=description,
            transaction_date=_accounting_date(transaction_date, self._settings),
            device_id=device_id or actor.device_id,
            rate_snapshot=rate_snapshot,
        )
        await self._assert_scope(actor, branch_id=branch_id)
        async with self._ledger_transaction(session) as active:
            await self._lock_accounts(active, *[entry.account_id for entry in lines])
            return await self._post(
                active,
                plan=plan,
                actor=actor,
                idempotency_key=idempotency_key,
                endpoint=endpoint,
            )

    async def post_exchange(
        self,
        *,
        transaction_type: str,
        reference_id: uuid.UUID,
        branch_id: uuid.UUID,
        from_currency_id: uuid.UUID,
        to_currency_id: uuid.UUID,
        from_amount: Decimal,
        exchange_rate: Decimal,
        from_cash_account_id: uuid.UUID,
        to_cash_account_id: uuid.UUID,
        fx_account_id: uuid.UUID | None = None,
        commission: Decimal = Decimal(0),
        commission_account_id: uuid.UUID | None = None,
        description: str | None = None,
        transaction_date: dt.datetime | None = None,
        device_id: uuid.UUID | None = None,
        rate_snapshot: RateSnapshot | None = None,
        actor: ActorContext,
        idempotency_key: uuid.UUID | None = None,
        endpoint: str | None = ENDPOINT_LEDGER_POSTING,
        session: AsyncSession | None = None,
    ) -> JournalEntryView:
        """Post the journal of a BUY or SELL (``ACCOUNTING_MODEL.md`` §6.2, §6.3).

        Quantities come from the document: ``from_amount`` is the quantity of
        ``from_currency`` the deal moves, ``exchange_rate`` is the applied quote
        (``to`` per 1 ``from``), and ``commission`` is charged in ``to_currency``. The
        functional value of every leg is then computed the way the accounting model
        defines it:

        * the currency the business **acquires** enters the books at the transaction price
          (``quantity x rate``, converted to functional units when ``to_currency`` is not
          the functional currency);
        * the currency the business **disposes** leaves at its **carrying rate** — the
          functional value per unit actually held, read from the ledger, never from a
          cache (``INSUFFICIENT_BALANCE`` when the position is empty);
        * the commission is income in ``to_currency``;
        * the difference between those legs is the realized FX result, posted to
          ``fx_account_id`` (account 4000 in the seeded chart).

        For the documented cases — either side being the functional currency — this
        reduces *exactly* to the model's arithmetic and its worked examples, which the
        suite asserts line by line.

        The **direction** is validated first, and on its own terms: both exchange types
        deliver a *foreign* currency (§6.2 "business acquires foreign currency", §6.3
        "business disposes foreign currency"), so a deal that delivers the functional
        currency, or that names one currency on both sides, is refused with
        ``EXCHANGE_DIRECTION_INVALID`` — a code of its own rather than the
        ``INSUFFICIENT_BALANCE`` or ``RATE_NOT_FOUND`` an invalid direction used to trip
        over, which told the caller the wrong story and could disappear entirely (a SELL
        that delivered the functional currency used to *post*).
        """
        transaction_type = _transaction_type(transaction_type)
        await self._authorize("EXCHANGE_TRANSACTION", actor)
        await self._assert_scope(actor, branch_id=branch_id)

        gross_rate = _positive(exchange_rate, field="exchange_rate")
        amount_from = _positive(from_amount, field="from_amount")
        commission_amount = _non_negative(commission, field="commission")
        moment = _accounting_date(transaction_date, self._settings)

        async with self._ledger_transaction(session) as active:
            # Every account this entry can touch is locked before the first read that
            # decides anything: the disposal's price and its quantity guard both come from
            # the account's own position, and a decision made from an unlocked read is a
            # decision two concurrent sellers could both make (§6.3).
            await self._lock_accounts(
                active,
                from_cash_account_id,
                to_cash_account_id,
                fx_account_id,
                commission_account_id,
            )
            currencies = CurrencyRepository(active)
            base = await _require_base_currency(active)
            from_currency = await _require_currency(
                currencies, from_currency_id, "from_currency_id"
            )
            to_currency = await _require_currency(currencies, to_currency_id, "to_currency_id")
            _assert_cash_quantity(
                amount=amount_from,
                currency=from_currency,
                field="from_amount",
                transaction_type=transaction_type,
            )
            _assert_exchange_direction(
                transaction_type=transaction_type,
                from_currency=from_currency,
                to_currency=to_currency,
                base=base,
            )

            # What one unit of the currency the business *receives* is worth in functional
            # units (see :meth:`_functional_rate`): the house's own quote for it against
            # the functional currency, in the direction of the deal.
            to_functional_rate = await self._functional_rate(
                active,
                currency=to_currency,
                base=base,
                branch_id=branch_id,
                at=moment,
                receiving=transaction_type == "SELL",
            )
            # The deal's arithmetic lives in one place (`compute_exchange_amounts`), so the
            # document service that orchestrates an exchange stores exactly the numbers the
            # ledger posts — there is no second formula to drift.
            computation = compute_exchange_amounts(
                transaction_type=transaction_type,
                from_amount=amount_from,
                exchange_rate=gross_rate,
                commission=commission_amount,
                from_decimal_places=from_currency.decimal_places,
                to_decimal_places=to_currency.decimal_places,
            )
            to_amount = computation.settlement_amount

            if transaction_type == "BUY":
                # ``ACCOUNTING_MODEL.md`` §6.2: the acquired currency enters the books at
                # its transaction price (``gross``, expressed per unit of the from
                # currency), and the currency paid out leaves the drawer at the rate
                # that drawer actually carries.
                acquired_rate = multiply_money(gross_rate, to_functional_rate)
                paid_carrying = await self._carrying_rate(
                    active,
                    account_id=to_cash_account_id,
                    currency_id=to_currency.id,
                    base_currency_id=base.id,
                    branch_id=branch_id,
                    disposing_quantity=to_amount,
                )
                lines = [
                    PostingLine(
                        account_id=from_cash_account_id,
                        currency_id=from_currency.id,
                        debit=multiply_money(amount_from, acquired_rate),
                        exchange_rate=acquired_rate,
                        description=f"BUY {from_currency.code} acquired",
                    ),
                    PostingLine(
                        account_id=to_cash_account_id,
                        currency_id=to_currency.id,
                        credit=multiply_money(to_amount, paid_carrying),
                        exchange_rate=paid_carrying,
                        description=f"BUY {to_currency.code} paid out",
                    ),
                ]
            else:
                # §6.3: the disposal leaves at its carrying rate — functional value per
                # unit actually held, read from the ledger, never from a cache.
                carrying = await self._carrying_rate(
                    active,
                    account_id=from_cash_account_id,
                    currency_id=from_currency.id,
                    base_currency_id=base.id,
                    branch_id=branch_id,
                    disposing_quantity=amount_from,
                )
                lines = [
                    PostingLine(
                        account_id=to_cash_account_id,
                        currency_id=to_currency.id,
                        debit=multiply_money(to_amount, to_functional_rate),
                        exchange_rate=to_functional_rate,
                        description=f"SELL {to_currency.code} received",
                    ),
                    PostingLine(
                        account_id=from_cash_account_id,
                        currency_id=from_currency.id,
                        credit=multiply_money(amount_from, carrying),
                        exchange_rate=carrying,
                        description=f"SELL {from_currency.code} delivered",
                    ),
                ]

            if commission_amount > 0:
                if commission_account_id is None:
                    raise ValidationError(
                        "A commission needs the income account that recognizes it.",
                        details={
                            "fields": [{"field": "commission_account_id", "code": "required"}]
                        },
                    )
                lines.append(
                    PostingLine(
                        account_id=commission_account_id,
                        currency_id=to_currency.id,
                        credit=multiply_money(
                            commission_amount, to_functional_rate, scale=base.decimal_places
                        ),
                        exchange_rate=to_functional_rate,
                        description=f"Commission on {transaction_type}",
                    )
                )

            lines = _close_with_fx_result(
                lines,
                fx_account_id=fx_account_id,
                functional_currency_id=base.id,
                reference=f"{transaction_type} {from_currency.code}/{to_currency.code}",
            )
            plan = _PostingPlan(
                reference_type="EXCHANGE_TRANSACTION",
                reference_id=reference_id,
                branch_id=branch_id,
                lines=tuple(lines),
                fingerprint=_fingerprint(
                    transaction_type=transaction_type,
                    reference_id=reference_id,
                    branch_id=branch_id,
                    from_currency_id=from_currency_id,
                    to_currency_id=to_currency_id,
                    from_amount=from_amount,
                    exchange_rate=gross_rate,
                    commission=commission_amount,
                    from_cash_account_id=from_cash_account_id,
                    to_cash_account_id=to_cash_account_id,
                    fx_account_id=fx_account_id,
                    commission_account_id=commission_account_id,
                    description=description,
                    transaction_date=transaction_date,
                ),
                description=description
                or f"{transaction_type} {from_amount} {from_currency.code} at {gross_rate}",
                transaction_date=moment,
                device_id=device_id or actor.device_id,
                rate_snapshot=rate_snapshot
                or RateSnapshot(
                    rate=gross_rate,
                    from_currency_id=from_currency.id,
                    to_currency_id=to_currency.id,
                    branch_id=branch_id,
                ),
            )
            return await self._post(
                active, plan=plan, actor=actor, idempotency_key=idempotency_key, endpoint=endpoint
            )

    async def post_cash_movement(
        self,
        *,
        movement_type: str,
        reference_id: uuid.UUID,
        branch_id: uuid.UUID,
        cash_account_id: uuid.UUID,
        counter_account_id: uuid.UUID,
        currency_id: uuid.UUID,
        amount: Decimal,
        exchange_rate: Decimal = Decimal(1),
        adjustment_sign: int | None = None,
        description: str | None = None,
        transaction_date: dt.datetime | None = None,
        device_id: uuid.UUID | None = None,
        rate_snapshot: RateSnapshot | None = None,
        actor: ActorContext,
        idempotency_key: uuid.UUID | None = None,
        endpoint: str | None = ENDPOINT_LEDGER_POSTING,
        session: AsyncSession | None = None,
    ) -> JournalEntryView:
        """Post the journal of a cash movement (``ACCOUNTING_MODEL.md`` §6.4, §6.1).

        ``IN``/``OUT``/``ADJUSTMENT``/``OPENING`` each need an explicit counter account —
        cash cannot appear from nowhere. ``CLOSING`` is a reconciliation snapshot and
        deliberately produces no journal entry here; a discrepancy is posted as an
        ``ADJUSTMENT`` (Phase 6), which is the only auditable way to state it.

        The two legs carry the same **functional** amount, which is what makes the entry
        balance exactly, but they are not always in the same currency. The model says so
        explicitly: an opening of 1,000 USD at 70 is ``Dr Cash USD 70,000 / Cr 6000
        Opening Offset 70,000`` where the offset line is in the functional currency at
        rate 1. The counter leg therefore takes its currency from *its own account* (a
        group account without a currency keeps the movement's), and its rate values that
        currency against the functional one. A USD drawer can also be adjusted against the
        AFN Cash Short/Over account on the same principle — the functional value is what
        must balance.
        """
        movement_type = _cash_movement_type(movement_type)
        reference_type = "OPENING_BALANCE" if movement_type == "OPENING" else "CASH_MOVEMENT"
        await self._authorize(reference_type, actor)
        await self._assert_scope(actor, branch_id=branch_id)

        if cash_account_id == counter_account_id:
            raise CashCounterAccountRequiredError(
                "A cash movement needs a counter account other than the cash account.",
                details={
                    "fields": [{"field": "counter_account_id", "code": "same_as_cash_account"}],
                    "cash_account_id": str(cash_account_id),
                },
            )
        movement_amount = _positive(amount, field="amount")
        rate = _positive(exchange_rate, field="exchange_rate")
        sign = _adjustment_sign(movement_type, adjustment_sign)
        moment = _accounting_date(transaction_date, self._settings)

        functional = multiply_money(movement_amount, rate)
        incoming = movement_type in ("IN", "OPENING") or (
            movement_type == "ADJUSTMENT" and sign > 0
        )
        async with self._ledger_transaction(session) as active:
            await self._lock_accounts(active, cash_account_id, counter_account_id)
            base = await _require_base_currency(active)
            counter_currency_id, counter_rate = await self._counter_leg_currency(
                active,
                counter_account_id=counter_account_id,
                movement_currency_id=currency_id,
                movement_rate=rate,
                base=base,
                branch_id=branch_id,
                at=moment,
            )
            cash_line = PostingLine(
                account_id=cash_account_id,
                currency_id=currency_id,
                debit=functional if incoming else Decimal(0),
                credit=Decimal(0) if incoming else functional,
                exchange_rate=rate,
                description=description,
            )
            counter_line = PostingLine(
                account_id=counter_account_id,
                currency_id=counter_currency_id,
                debit=Decimal(0) if incoming else functional,
                credit=functional if incoming else Decimal(0),
                exchange_rate=counter_rate,
                description=description,
            )
            plan = _PostingPlan(
                reference_type=reference_type,
                reference_id=reference_id,
                branch_id=branch_id,
                lines=(cash_line, counter_line),
                fingerprint=_fingerprint(
                    movement_type=movement_type,
                    reference_id=reference_id,
                    branch_id=branch_id,
                    cash_account_id=cash_account_id,
                    counter_account_id=counter_account_id,
                    currency_id=currency_id,
                    amount=movement_amount,
                    exchange_rate=rate,
                    adjustment_sign=sign if movement_type == "ADJUSTMENT" else None,
                    description=description,
                    transaction_date=transaction_date,
                ),
                description=description or f"Cash {movement_type}",
                transaction_date=moment,
                device_id=device_id or actor.device_id,
                rate_snapshot=rate_snapshot
                or RateSnapshot(rate=rate, from_currency_id=currency_id, branch_id=branch_id),
            )
            return await self._post(
                active, plan=plan, actor=actor, idempotency_key=idempotency_key, endpoint=endpoint
            )

    async def post_expense(
        self,
        *,
        reference_id: uuid.UUID,
        branch_id: uuid.UUID,
        expense_account_id: uuid.UUID,
        credit_account_id: uuid.UUID,
        currency_id: uuid.UUID,
        amount: Decimal,
        exchange_rate: Decimal = Decimal(1),
        description: str | None = None,
        transaction_date: dt.datetime | None = None,
        device_id: uuid.UUID | None = None,
        rate_snapshot: RateSnapshot | None = None,
        actor: ActorContext,
        idempotency_key: uuid.UUID | None = None,
        endpoint: str | None = ENDPOINT_LEDGER_POSTING,
        session: AsyncSession | None = None,
    ) -> JournalEntryView:
        """Post an expense (``ACCOUNTING_MODEL.md`` §6.5).

        ``Dr <expense account>`` / ``Cr <the account that pays it>`` — the cash drawer for
        an immediate payment, a payable account for an accrued one. Both legs are in the
        expense's own currency: settling an expense from a *different* currency is a cash
        policy decision (which drawer, at which rate) that belongs with the cash module,
        and inventing it here would produce numbers nobody agreed to.
        """
        await self._authorize("EXPENSE", actor)
        await self._assert_scope(actor, branch_id=branch_id)
        expense_amount = _positive(amount, field="amount")
        rate = _positive(exchange_rate, field="exchange_rate")
        if expense_account_id == credit_account_id:
            raise ValidationError(
                "An expense needs a credit account other than the expense account.",
                details={"fields": [{"field": "credit_account_id", "code": "same_as_expense"}]},
            )
        functional = multiply_money(expense_amount, rate)
        plan = _PostingPlan(
            reference_type="EXPENSE",
            reference_id=reference_id,
            branch_id=branch_id,
            fingerprint=_fingerprint(
                reference_id=reference_id,
                branch_id=branch_id,
                expense_account_id=expense_account_id,
                credit_account_id=credit_account_id,
                currency_id=currency_id,
                amount=expense_amount,
                exchange_rate=rate,
                description=description,
                transaction_date=transaction_date,
            ),
            lines=(
                PostingLine(
                    account_id=expense_account_id,
                    currency_id=currency_id,
                    debit=functional,
                    exchange_rate=rate,
                    description=description,
                ),
                PostingLine(
                    account_id=credit_account_id,
                    currency_id=currency_id,
                    credit=functional,
                    exchange_rate=rate,
                    description=description,
                ),
            ),
            description=description or "Expense",
            transaction_date=_accounting_date(transaction_date, self._settings),
            device_id=device_id or actor.device_id,
            rate_snapshot=rate_snapshot
            or RateSnapshot(rate=rate, from_currency_id=currency_id, branch_id=branch_id),
        )
        async with self._ledger_transaction(session) as active:
            await self._lock_accounts(active, expense_account_id, credit_account_id)
            return await self._post(
                active, plan=plan, actor=actor, idempotency_key=idempotency_key, endpoint=endpoint
            )

    async def reverse_journal_entry(
        self,
        *,
        journal_entry_id: uuid.UUID,
        reason: str,
        actor: ActorContext,
        transaction_date: dt.datetime | None = None,
        device_id: uuid.UUID | None = None,
        idempotency_key: uuid.UUID | None = None,
        endpoint: str | None = ENDPOINT_LEDGER_POSTING,
        session: AsyncSession | None = None,
        authority: Permission | None = None,
    ) -> JournalEntryView:
        """Reverse a posted entry: a mirror entry, never a deletion (PART 22).

        The mirror swaps debit and credit line by line and keeps every account, currency
        and rate, so it reverses the *quantities* as well as the functional amounts
        (``foreign_amount`` is generated from the swapped pair and comes back equal). The
        original keeps existing exactly as posted; the link is structural
        (``reversal_of_id``, plus the ``REVERSAL``/original-id reference that
        ``ux_journal_entries_one_per_reference`` allows only once).

        Refused: reversing a reversal (post a new document instead), and reversing an
        entry that already has one (409 ``ALREADY_REVERSED``). An entry in an
        *inactive* branch can still be reversed — a branch being retired must not make its
        history uncorrectable.
        """
        if not reason or not reason.strip():
            raise ValidationError(
                "A reversal needs a reason: the auditor reads it.",
                details={"fields": [{"field": "reason", "code": "required"}]},
            )
        if actor.user_id is None:
            raise PermissionDeniedError(
                "A ledger entry must name the user who posted it.",
                details={"reason": "ACTOR_REQUIRED"},
            )

        async with self._ledger_transaction(session) as active:
            entries = JournalEntryRepository(active)
            original = await entries.get_entry(journal_entry_id, for_update=True)
            if original is None:
                raise ResourceNotFoundError(
                    "That journal entry does not exist.",
                    details={"resource": "journal_entry", "id": str(journal_entry_id)},
                )
            if original.reference_type in NON_REVERSIBLE_REFERENCE_TYPES:
                raise ReversalError(
                    "A reversal cannot itself be reversed; post a new document instead.",
                    details={
                        "journal_entry_id": str(original.id),
                        "reference_type": original.reference_type,
                    },
                )
            existing = await entries.find_reversal_of(original.id)
            if existing is not None:
                raise AlreadyReversedError(
                    "This entry has already been reversed.",
                    details={
                        "journal_entry_id": str(original.id),
                        "reversal_journal_entry_id": str(existing.id),
                        "reversed_at": existing.created_at.isoformat(),
                    },
                )

            authority = self._reversal_authority(
                reference_type=original.reference_type, requested=authority
            )
            await self._authorize_permission(
                actor,
                permission=authority,
                context={
                    "journal_entry_id": str(original.id),
                    "reference_type": original.reference_type,
                },
            )
            await self._assert_scope(
                actor,
                branch_id=original.branch_id,
                context={"journal_entry_id": str(original.id)},
            )

            moment = _accounting_date(transaction_date, self._settings)
            if moment < original.transaction_date:
                raise ValidationError(
                    "A reversal cannot be dated before the entry it reverses.",
                    details={
                        "fields": [{"field": "transaction_date", "code": "before_original"}],
                        "original_transaction_date": original.transaction_date.isoformat(),
                    },
                )

            original_lines = await entries.lines_of(original.id)
            await self._lock_accounts(active, *[line.account_id for line in original_lines])
            mirrored = tuple(
                PostingLine(
                    account_id=line.account_id,
                    currency_id=line.currency_id,
                    debit=line.credit,
                    credit=line.debit,
                    exchange_rate=line.exchange_rate,
                    description=line.description,
                )
                for line in original_lines
            )
            totals = self.validate_balanced_entry(mirrored)
            plan = _PostingPlan(
                reference_type="REVERSAL",
                reference_id=original.id,
                branch_id=original.branch_id,
                lines=mirrored,
                fingerprint={
                    "reversal_of_id": original.id,
                    "reason": reason.strip(),
                    "branch_id": original.branch_id,
                    "transaction_date": transaction_date,
                },
                description=reason.strip(),
                transaction_date=moment,
                device_id=device_id or actor.device_id,
                rate_snapshot=None,
            )
            audit_extra = {
                "reversal_of_id": str(original.id),
                "reversal_reason": reason.strip(),
                "original_reference_type": original.reference_type,
                "mirrored_lines": totals.lines,
                "total_debit": format_decimal(totals.debit),
                "total_credit": format_decimal(totals.credit),
                "original_transaction_date": original.transaction_date.isoformat(),
            }
            return await self._post(
                active,
                plan=plan,
                actor=actor,
                idempotency_key=idempotency_key,
                endpoint=endpoint,
                audit_action=AuditAction.JOURNAL_REVERSED,
                audit_extra=audit_extra,
            )

    async def reverse_transaction(
        self,
        *,
        reference_type: str,
        reference_id: uuid.UUID,
        reason: str,
        actor: ActorContext,
        session: AsyncSession | None = None,
        transaction_date: dt.datetime | None = None,
        device_id: uuid.UUID | None = None,
        idempotency_key: uuid.UUID | None = None,
        endpoint: str | None = ENDPOINT_LEDGER_POSTING,
        authority: Permission | None = None,
    ) -> JournalEntryView:
        """Reverse the journal of one business document, identified by its reference.

        This is the entry point a later phase calls after it has flipped its own document
        to ``REVERSED``: the document's lifecycle and its reversal link live in the table
        that owns the document (``exchange_transactions``, ``expenses``, …), while the
        ledger consequence is the mirror entry this method writes. Passing ``session``
        keeps the document update and the journal in one transaction (PART 20).

        Refused when the document has no journal, when it was already reversed, and when
        the document is a reversal itself (a reversal is not reversible; post a new
        document instead).
        """
        reference_type = _reference_type(reference_type)
        if reference_type == "REVERSAL":
            raise ReversalError(
                "A reversal is identified by the entry it mirrors, not by a reference.",
                details={"reference_type": reference_type, "reference_id": str(reference_id)},
            )
        async with self._ledger_transaction(session) as active:
            entry = await JournalEntryRepository(active).find_by_reference(
                reference_type=reference_type, reference_id=reference_id
            )
            if entry is None:
                raise ResourceNotFoundError(
                    "That document has no posted journal entry to reverse.",
                    details={
                        "reference_type": reference_type,
                        "reference_id": str(reference_id),
                    },
                )
            entry_id = entry.id
        return await self.reverse_journal_entry(
            journal_entry_id=entry_id,
            reason=reason,
            actor=actor,
            transaction_date=transaction_date,
            device_id=device_id,
            idempotency_key=idempotency_key,
            endpoint=endpoint,
            session=session,
            authority=authority,
        )

    # ================================================================== reading
    async def get_journal_entry(
        self, *, entry_id: uuid.UUID, actor: ActorContext, with_lines: bool = True
    ) -> JournalEntryView:
        """One entry with its lines. Out of an actor's scope it does not exist (404)."""
        async with self._database.session() as session:
            entries = JournalEntryRepository(session)
            row = await entries.entry_row(entry_id)
            if row is None:
                raise ResourceNotFoundError(
                    "That journal entry does not exist.",
                    details={"resource": "journal_entry", "id": str(entry_id)},
                )
            if not self._can_see_branch(actor, row["branch_id"]):
                raise ResourceNotFoundError(
                    "That journal entry does not exist.",
                    details={"resource": "journal_entry", "id": str(entry_id)},
                )
            lines = tuple(
                _line_view(line)
                for line in (await entries.line_rows(entry_id) if with_lines else ())
            )
            return _entry_view(row, lines=lines)

    async def list_journal_entries(
        self,
        *,
        actor: ActorContext,
        reference_type: str | None = None,
        reference_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[JournalEntryView], int]:
        """The journal, newest first, within the actor's branch scope."""
        branch_ids = await self._read_scope(actor, branch_id=branch_id)
        async with self._database.session() as session:
            rows, total = await JournalEntryRepository(session).list_entries(
                reference_type=reference_type,
                reference_id=reference_id,
                branch_ids=branch_ids,
                from_=from_,
                to=to,
                limit=limit,
                offset=offset,
            )
            return [_entry_view(row) for row in rows], total

    async def get_account_balance(
        self,
        *,
        account_id: uuid.UUID,
        actor: ActorContext,
        currency_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
        as_of: dt.datetime | None = None,
    ) -> AccountBalanceView:
        """An account's debit/credit totals and signed balance, from the ledger.

        Read from ``journal_lines`` rather than the ``account_balances`` cache: the cache is
        a rebuildable convenience, and a report that can disagree with the ledger is worse
        than a slow report (invariant I-2).

        The totals are scoped to the caller's branch — a branch-bound accountant reads
        their branch's share of a group-level account, not the whole company's, and asking
        for another branch is refused (403) rather than silently widened.
        """
        branch_ids = await self._read_scope(actor, branch_id=branch_id)
        async with self._database.session() as session:
            account = await AccountRepository(session).get(account_id)
            if account is None:
                raise ResourceNotFoundError(
                    "That account does not exist.",
                    details={"resource": "account", "id": str(account_id)},
                )
            if not self._can_see_branch(actor, account.branch_id):
                raise ResourceNotFoundError(
                    "That account does not exist.",
                    details={"resource": "account", "id": str(account_id)},
                )
            if account.branch_id is not None and branch_ids is not None:
                # A branch-scoped account holds only its own branch's money, whatever the
                # caller asked for: the account's own scope is the tighter one.
                branch_ids = [account.branch_id]
            rows = await LedgerRepository(session).account_totals(
                account_id=account_id,
                branch_ids=branch_ids,
                currency_id=currency_id,
                as_of=as_of,
            )
            return AccountBalanceView(
                account_id=account.id,
                account_code=account.code,
                account_name=account.name,
                account_type=account.account_type,
                normal_balance=(account.normal_balance or None),
                is_active=account.is_active,
                branch_ids=tuple(branch_ids) if branch_ids is not None else None,
                as_of=as_of,
                rows=tuple(
                    AccountBalanceRow(
                        currency_id=row["currency_id"],
                        currency_code=row["currency_code"],
                        debit_total=row["debit_total"],
                        credit_total=row["credit_total"],
                        balance=_functional_balance(
                            account.normal_balance, row["debit_total"], row["credit_total"]
                        ),
                        entry_count=int(row["entry_count"]),
                        last_posted_at=row["last_posted_at"],
                    )
                    for row in rows
                ),
            )

    async def get_trial_balance(
        self,
        *,
        actor: ActorContext,
        branch_id: uuid.UUID | None = None,
        account_type: str | None = None,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
        include_inactive: bool = True,
    ) -> TrialBalanceView:
        """Every account's totals with Σdebit and Σcredit, which must be equal."""
        branch_ids = await self._read_scope(actor, branch_id=branch_id)
        effective_branch = branch_ids[0] if branch_ids else None
        async with self._database.session() as session:
            ledger = LedgerRepository(session)
            rows = await ledger.trial_balance(
                branch_id=effective_branch,
                account_type=account_type,
                from_=from_,
                to=to,
                include_inactive=include_inactive,
            )
            totals = await ledger.ledger_totals(branch_id=effective_branch, from_=from_, to=to)
            return TrialBalanceView(
                rows=tuple(
                    TrialBalanceRow(
                        account_id=row["account_id"],
                        account_code=row["account_code"],
                        account_name=row["account_name"],
                        account_type=row["account_type"],
                        normal_balance=(row["normal_balance"] or "").strip() or None,
                        currency_id=row["currency_id"],
                        currency_code=row["currency_code"],
                        total_debit=row["total_debit"],
                        total_credit=row["total_credit"],
                        net_debit=row["net_debit"],
                        balance=_functional_balance(
                            row["normal_balance"], row["total_debit"], row["total_credit"]
                        ),
                        entry_count=int(row["entry_count"]),
                    )
                    for row in rows
                ),
                total_debit=totals["total_debit"],
                total_credit=totals["total_credit"],
                generated_at=dt.datetime.now(tz=dt.UTC),
                branch_id=effective_branch,
                account_type=account_type,
                from_date=from_,
                to_date=to,
                include_inactive=include_inactive,
            )

    # ================================================================= internals
    async def _post(
        self,
        session: AsyncSession,
        *,
        plan: _PostingPlan,
        actor: ActorContext,
        idempotency_key: uuid.UUID | None,
        endpoint: str | None,
        audit_action: AuditAction = AuditAction.JOURNAL_POSTED,
        audit_extra: Mapping[str, Any] | None = None,
    ) -> JournalEntryView:
        """Validate, insert, audit and (optionally) make idempotent one posting plan."""
        if actor.user_id is None:
            raise PermissionDeniedError(
                "A ledger entry must name the user who posted it.",
                details={"reason": "ACTOR_REQUIRED"},
            )
        totals = self.validate_balanced_entry(plan.lines)
        accounts = await self._check_ledger_context(session, plan=plan)
        if plan.guard_inventory:
            await self._assert_inventory_positions(session, plan=plan, accounts=accounts)
        guard = await self._claim_idempotency(
            session, plan=plan, actor=actor, idempotency_key=idempotency_key, endpoint=endpoint
        )
        if isinstance(guard, JournalEntryView):  # a replay: no second posting
            return guard

        entries = JournalEntryRepository(session)
        if (
            plan.reference_id is not None
            and plan.reference_type in REFERENCE_TYPES_REQUIRING_DOCUMENT
        ):
            existing = await entries.find_by_reference(
                reference_type=plan.reference_type, reference_id=plan.reference_id
            )
            if existing is not None:
                raise DuplicateResourceError(
                    "This document has already been posted to the ledger.",
                    details={
                        "reference_type": plan.reference_type,
                        "reference_id": str(plan.reference_id),
                        "journal_entry_id": str(existing.id),
                    },
                )

        entry = JournalEntry(
            reference_type=plan.reference_type,
            reference_id=plan.reference_id,
            description=plan.description,
            transaction_date=plan.transaction_date,
            created_by=actor.user_id,
            branch_id=plan.branch_id,
            device_id=plan.device_id,
            reversal_of_id=plan.reference_id if plan.reference_type == "REVERSAL" else None,
        )
        entries.add_entry(entry)
        await entries.flush()
        entries.add_lines(
            [
                JournalLine(
                    journal_entry_id=entry.id,
                    account_id=line.account_id,
                    debit=line.debit,
                    credit=line.credit,
                    currency_id=line.currency_id,
                    exchange_rate=line.exchange_rate,
                    description=line.description,
                )
                # Sorted so two concurrent postings touch ``account_balances`` in the same
                # order: the cache trigger upserts one row per account, and a differing
                # order between two transactions is what turns a busy ledger into
                # deadlocks. Reads have their own explicit order and do not depend on it.
                for line in sorted(
                    plan.lines, key=lambda item: (str(item.account_id), str(item.currency_id))
                )
            ]
        )
        await entries.flush()

        view = await self._load_view(session, entry.id)
        AuditService(session).record(
            action=audit_action,
            entity_type="journal_entry",
            entity_id=entry.id,
            new_data={
                "reference_type": entry.reference_type,
                "reference_id": str(entry.reference_id) if entry.reference_id else None,
                "description": entry.description,
                "transaction_date": entry.transaction_date.isoformat(),
                "branch_id": str(entry.branch_id) if entry.branch_id else None,
                "lines": totals.lines,
                "total_debit": format_decimal(totals.debit),
                "total_credit": format_decimal(totals.credit),
                "rate_snapshot": plan.rate_snapshot.as_payload() if plan.rate_snapshot else None,
                "line_detail": [
                    {
                        "account_id": str(line.account_id),
                        "currency_id": str(line.currency_id),
                        "debit": format_decimal(line.debit),
                        "credit": format_decimal(line.credit),
                        "exchange_rate": format_decimal(line.exchange_rate),
                    }
                    for line in plan.lines
                ],
                **(dict(audit_extra) if audit_extra else {}),
            },
            actor=actor,
        )
        if guard is not None:
            guard.complete(
                status_code=201,
                body=view.to_payload(),
                resource_type="journal_entry",
                resource_id=entry.id,
            )
        return view

    async def _claim_idempotency(
        self,
        session: AsyncSession,
        *,
        plan: _PostingPlan,
        actor: ActorContext,
        idempotency_key: uuid.UUID | None,
        endpoint: str | None,
    ) -> IdempotencyGuard | JournalEntryView | None:
        """Claim the key, or return the entry a previous call with it already posted."""
        if idempotency_key is None:
            return None
        if not endpoint:
            raise ValidationError(
                "An Idempotency-Key is only meaningful with the endpoint it was used on.",
                details={"fields": [{"field": "endpoint", "code": "required"}]},
            )
        assert actor.user_id is not None  # guaranteed by the caller
        guard = IdempotencyGuard(
            session,
            IdempotencyRequest(
                key=idempotency_key,
                user_id=actor.user_id,
                endpoint=endpoint,
                request_hash=canonical_request_hash(dict(plan.fingerprint)),
                device_id=actor.device_id,
            ),
        )
        replay = await guard.claim()
        return guard if replay is None else JournalEntryView.from_payload(replay.body)

    async def _check_ledger_context(
        self, session: AsyncSession, *, plan: _PostingPlan
    ) -> dict[uuid.UUID, Account]:
        """Every rule a line must satisfy before it can touch the ledger.

        This is where "valid account/currency relationships" and "branch boundaries" stop
        being slogans: the account must exist, be active, be **postable** (a grouping
        account is a subtotal, not a place to post), its currency must be the line's
        currency, and its branch must be the entry's branch (or the account must be
        group-wide). A posting to a branch that is not active is refused — trading at a
        closed branch is how a ledger stops matching reality.

        Returns the accounts it loaded, so a caller that needs the same rows (the generic
        door's inventory guard) reads each account once per posting instead of twice.
        """
        if plan.reference_type in REFERENCE_TYPES_REQUIRING_DOCUMENT and plan.reference_id is None:
            raise ValidationError(
                "A posted document must reference the document it belongs to.",
                details={"fields": [{"field": "reference_id", "code": "required"}]},
            )
        if plan.branch_id is not None:
            branch = await BranchRepository(session).get(plan.branch_id)
            if branch is None:
                raise ResourceNotFoundError(
                    "That branch does not exist.",
                    details={"fields": [{"field": "branch_id", "code": "not_found"}]},
                )
            if not branch.is_active and plan.reference_type != "REVERSAL":
                raise ValidationError(
                    "A closed branch cannot receive new postings.",
                    details={"fields": [{"field": "branch_id", "code": "inactive"}]},
                )

        # Serialise postings that touch the same accounts, before anything is read from
        # them. Some rules are decided from derived state (a disposal may not deliver more
        # than the position holds, §6.3), and read-then-write is only safe under a lock.
        # The lock order is the insert order, so two postings sharing accounts queue
        # instead of deadlocking.
        await JournalEntryRepository(session).lock_accounts(
            [line.account_id for line in plan.lines]
        )

        # One lookup per distinct id, cached: a multi-line entry reads a handful of rows
        # rather than repeating a query per line, and the loop below stays about rules.
        accounts: dict[uuid.UUID, Account] = {}
        currencies: dict[uuid.UUID, Currency] = {}
        for index, line in enumerate(plan.lines):
            account = accounts.get(line.account_id)
            if account is None:
                loaded = await AccountRepository(session).get(line.account_id)
                if loaded is not None:
                    accounts[line.account_id] = loaded
                account = loaded
            if account is None:
                raise ResourceNotFoundError(
                    "That account does not exist.",
                    details={
                        "fields": [{"field": f"lines[{index}].account_id", "code": "not_found"}]
                    },
                )
            if not account.is_active:
                raise ValidationError(
                    "That account is not active.",
                    details={
                        "fields": [{"field": f"lines[{index}].account_id", "code": "inactive"}]
                    },
                )
            if not account.is_postable:
                raise ValidationError(
                    "That account groups a subtree and cannot be posted to.",
                    details={
                        "fields": [{"field": f"lines[{index}].account_id", "code": "not_postable"}]
                    },
                )
            if account.currency_id is not None and account.currency_id != line.currency_id:
                raise ValidationError(
                    "The line's currency is not the account's currency.",
                    details={
                        "fields": [
                            {"field": f"lines[{index}].currency_id", "code": "currency_mismatch"}
                        ],
                        "account_currency_id": str(account.currency_id),
                    },
                )
            if (
                account.branch_id is not None
                and plan.branch_id is not None
                and account.branch_id != plan.branch_id
            ):
                raise ValidationError(
                    "That account belongs to another branch.",
                    details={
                        "fields": [
                            {"field": f"lines[{index}].account_id", "code": "branch_mismatch"}
                        ],
                        "account_branch_id": str(account.branch_id),
                    },
                )
            currency = currencies.get(line.currency_id)
            if currency is None:
                loaded_currency = await CurrencyRepository(session).get(line.currency_id)
                if loaded_currency is not None:
                    currencies[line.currency_id] = loaded_currency
                currency = loaded_currency
            if currency is None:
                raise ResourceNotFoundError(
                    "That currency does not exist.",
                    details={
                        "fields": [{"field": f"lines[{index}].currency_id", "code": "not_found"}]
                    },
                )
            if not currency.is_active:
                raise CurrencyInactiveError(
                    details={
                        "fields": [{"field": f"lines[{index}].currency_id", "code": "inactive"}]
                    }
                )

        return accounts

    async def _functional_rate(
        self,
        session: AsyncSession,
        *,
        currency: Currency,
        base: Currency,
        branch_id: uuid.UUID | None,
        at: dt.datetime,
        receiving: bool,
    ) -> Decimal:
        """Functional units per one unit of ``currency``.

        The functional currency is itself 1 by definition (there is nothing to convert).
        Any other currency is valued with the *house's own quote* for it against the base
        currency, resolved through the Phase 0 ``resolve_exchange_rate`` function — and in
        the direction of the deal: what the business receives is valued at the rate it
        buys that currency, what it delivers at the rate it sells it. A currency without a
        quote in force cannot be valued, and pretending otherwise would invent a number:
        the posting is refused with ``RATE_NOT_FOUND``.
        """
        if currency.id == base.id:
            return Decimal(1)
        quote = await ExchangeRateRepository(session).resolve(
            from_currency_id=currency.id, to_currency_id=base.id, branch_id=branch_id, at=at
        )
        if quote is None:
            raise RateNotFoundError(
                f"No {currency.code}/{base.code} quote is in force to value this posting.",
                details={
                    "currency_code": currency.code,
                    "functional_currency_code": base.code,
                    "branch_id": str(branch_id) if branch_id else None,
                    "hint": "Publish a quote for this pair at this branch (or globally).",
                },
            )
        quote_row = cast("Mapping[str, Any]", quote)
        rate = quote_row["buy_rate"] if receiving else quote_row["sell_rate"]
        return _positive(Decimal(rate), field=f"{currency.code}_functional_rate")

    async def _counter_leg_currency(
        self,
        session: AsyncSession,
        *,
        counter_account_id: uuid.UUID,
        movement_currency_id: uuid.UUID,
        movement_rate: Decimal,
        base: Currency,
        branch_id: uuid.UUID | None,
        at: dt.datetime,
    ) -> tuple[uuid.UUID, Decimal]:
        """The currency and rate of a cash movement's counter leg.

        Three cases, in the order the accounting model meets them:

        * the counter account has no currency of its own (``3000`` capital, ``6000`` opening
          offset, an expense account) → the **functional** currency at rate 1. Such an
          account records functional value only; denominating its leg in the movement's
          currency would claim the house paid out a quantity of that currency, and
          ``journal_lines.foreign_amount`` would then state a physical fact that never
          happened. §6.1 fixes exactly this: ``Cr 6000 Opening Offset func = qty * rate
          (currency = AFN, rate = 1)``;
        * the counter account shares the movement's currency (``Cr Cash AFN`` beside a USD
          deposit) → the same currency and rate, so the two legs mirror each other exactly;
        * the counter account is in some *other* currency → that currency, valued with the
          house's own quote for it, exactly as a functional rate is derived elsewhere.

        In every case the counter leg carries the same **functional** amount as the cash
        leg, so a movement between two accounts can never need an FX result line: it
        converts nothing.
        """
        account = await AccountRepository(session).get(counter_account_id)
        counter_currency_id = account.currency_id if account is not None else None
        if counter_currency_id is None or counter_currency_id == base.id:
            return base.id, Decimal(1)
        if counter_currency_id == movement_currency_id:
            return movement_currency_id, movement_rate
        counter_currency = await _require_currency(
            CurrencyRepository(session), counter_currency_id, "counter_account_id"
        )
        counter_rate = await self._functional_rate(
            session,
            currency=counter_currency,
            base=base,
            branch_id=branch_id,
            at=at,
            receiving=True,
        )
        return counter_currency_id, counter_rate

    async def _carrying_rate(
        self,
        session: AsyncSession,
        *,
        account_id: uuid.UUID,
        currency_id: uuid.UUID,
        base_currency_id: uuid.UUID,
        branch_id: uuid.UUID | None,
        disposing_quantity: Decimal | None = None,
    ) -> Decimal:
        """Functional value per unit actually held in an inventory account (§6.3).

        The functional currency carries at exactly 1 — by definition, not by measurement:
        one afghani is one afghani however much of it the drawer holds. Deriving it from
        the ledger would divide by a quantity that is zero whenever the drawer is empty
        and would return a *negative* rate whenever the drawer is overdrawn, both of which
        are accounting nonsense.

        A foreign currency carries at (functional value of its lines ÷ units held), read
        from the immutable ledger. A disposal from an empty position is refused here with
        ``INSUFFICIENT_BALANCE``, and — when the caller states how much it is delivering —
        so is a disposal of **more units than the position holds**. The ledger quantity is
        the branch's physical position for that currency (§8's identity against
        ``v_cash_position``): letting a drawer go negative would book a delivery of money
        the branch does not have. The physical constraint on ``cash_movements``
        (``NEX01``) would refuse the matching cash row at commit, but the ledger door is
        the one every caller walks through, so it refuses first and explains why.
        """
        position = await LedgerRepository(session).position(
            account_id=account_id, branch_id=branch_id
        )
        quantity = Decimal(position["foreign_quantity"])
        value = Decimal(position["functional_balance"])
        if currency_id == base_currency_id:
            # The functional currency carries at 1 by definition, so there is no rate to
            # derive — but the *quantity* still has to be there. A drawer that holds 500
            # afghani cannot pay out 700, and until this guard existed that delivery was
            # only stopped by ``ct_cash_movements_non_negative`` (``NEX01``) at COMMIT,
            # which reaches the caller as a server error instead of the 409 an operator can
            # act on. The foreign branch below has always refused this; the functional
            # branch now refuses it the same way, from the same locked read.
            if disposing_quantity is not None and disposing_quantity > quantity:
                raise InsufficientBalanceError(
                    "This branch does not hold that much of the currency being delivered.",
                    details={
                        "account_id": str(account_id),
                        "disposing_quantity": format_decimal(disposing_quantity),
                        "foreign_quantity": format_decimal(quantity),
                        "shortfall": format_decimal(money_difference(disposing_quantity, quantity)),
                        "reason": "QUANTITY_EXCEEDED",
                    },
                )
            return Decimal(1)
        if quantity <= 0:
            raise InsufficientBalanceError(
                "This branch holds no position to deliver from.",
                details={
                    "account_id": str(account_id),
                    "foreign_quantity": format_decimal(quantity),
                    "functional_balance": format_decimal(value),
                    "reason": "NO_POSITION",
                },
            )
        if disposing_quantity is not None and disposing_quantity > quantity:
            raise InsufficientBalanceError(
                "This branch does not hold that much of the currency being delivered.",
                details={
                    "account_id": str(account_id),
                    "foreign_quantity": format_decimal(quantity),
                    "disposing_quantity": format_decimal(disposing_quantity),
                    "shortfall": format_decimal(money_difference(disposing_quantity, quantity)),
                    "reason": "QUANTITY_EXCEEDED",
                },
            )
        if value <= 0:
            # Units are held but their recorded functional value is zero or negative:
            # the account's history is internally inconsistent (only a posting that
            # bypassed the service can produce this), and dividing would hand a negative
            # or zero price to the next disposal. Refuse instead of pricing money wrongly.
            raise DataIntegrityError(
                "The ledger position of this account cannot price a disposal.",
                details={
                    "account_id": str(account_id),
                    "foreign_quantity": format_decimal(quantity),
                    "functional_balance": format_decimal(value),
                    "reason": "NON_POSITIVE_CARRYING_VALUE",
                },
            )
        return divide_money(value, quantity)

    async def _assert_inventory_positions(
        self,
        session: AsyncSession,
        *,
        plan: _PostingPlan,
        accounts: Mapping[uuid.UUID, Account],
    ) -> None:
        """No manual posting may deliver more of a currency than the branch holds.

        This is §6.3's disposal guard applied to the **generic** door. ``_carrying_rate``
        enforces it for a SELL, where the ledger prices the delivery itself; a manual entry
        states its own rate instead, so the guard has to be stated in quantities: a line's
        contribution to a position is ``(debit - credit) / rate`` — exactly the expression
        PostgreSQL uses to generate ``journal_lines.foreign_amount`` — and the sum over an
        account's lines may not take that account's holding below zero (invariant I-5).

        Only *inventory* accounts are guarded: an asset account **bound to a currency**, the
        model's definition of a place that holds a position (§2). A functional or control
        account (``3000`` capital, ``2000`` customer advance, ``1100`` transit) records
        functional value and has no physical quantity to run out of; a liability that goes
        negative is a receivable, not a missing banknote. The guard therefore refuses
        exactly what the physical constraint refuses, and nothing else.

        The accounts are already locked by ``_check_ledger_context`` (in the same order the
        lines are inserted), so the position read here cannot be overtaken by a concurrent
        posting: two manual disposals of one drawer queue and the second one sees the first
        one's result.
        """
        disposals: dict[uuid.UUID, Decimal] = {}
        for line in plan.lines:
            account = accounts.get(line.account_id)
            if account is None or account.currency_id is None:
                continue
            if (account.account_type or "").strip().upper() != "ASSET":
                continue
            # One line's physical contribution, quantized the way the stored generated
            # column is: a debit adds units, a credit removes them.
            contribution = divide_money(line.debit - line.credit, line.exchange_rate)
            disposals[line.account_id] = disposals.get(line.account_id, Decimal(0)) + contribution

        ledger = LedgerRepository(session)
        for account_id, contribution in disposals.items():
            if contribution >= 0:
                continue
            position = await ledger.position(account_id=account_id, branch_id=plan.branch_id)
            held = Decimal(position["foreign_quantity"])
            if held + contribution >= 0:
                continue
            delivered = contribution.copy_abs()
            details: dict[str, Any] = {
                "account_id": str(account_id),
                "foreign_quantity": format_decimal(held),
                "disposing_quantity": format_decimal(delivered),
                "branch_id": str(plan.branch_id) if plan.branch_id else None,
            }
            if held <= 0:
                details["reason"] = "NO_POSITION"
                raise InsufficientBalanceError(
                    "This branch holds no position to deliver from.", details=details
                )
            details["shortfall"] = format_decimal(money_difference(delivered, held))
            details["reason"] = "QUANTITY_EXCEEDED"
            raise InsufficientBalanceError(
                "This branch does not hold that much of the currency being delivered.",
                details=details,
            )

    async def _load_view(
        self, session: AsyncSession, entry_id: uuid.UUID, *, with_lines: bool = True
    ) -> JournalEntryView:
        """Read an entry back through the same projection the API uses."""
        entries = JournalEntryRepository(session)
        row = await entries.entry_row(entry_id)
        if row is None:  # pragma: no cover - defensive: the entry was just inserted
            raise DataIntegrityError(
                "The journal entry could not be read back after posting.",
                details={"journal_entry_id": str(entry_id)},
            )
        lines = tuple(
            _line_view(line) for line in (await entries.line_rows(entry_id) if with_lines else ())
        )
        return _entry_view(row, lines=lines)

    # ======================================================= authorisation/scope
    async def _authorize(self, reference_type: str, actor: ActorContext) -> None:
        permission = POSTING_AUTHORITY.get(reference_type, Permission.ACCOUNTS_MANAGE)
        await self._authorize_permission(
            actor, permission=permission, context={"reference_type": reference_type}
        )

    @staticmethod
    def _reversal_authority(*, reference_type: str, requested: Permission | None) -> Permission:
        """Which permission authorises undoing an entry of this kind.

        Without ``requested`` this is the contract's default door for the document type. A
        caller may instead name one of the doors the document actually has (see
        ``REVERSAL_AUTHORITY_CHOICES``); naming anything else is a defect in the caller, not
        a shortcut — the ledger still checks the permission against the actor, so this
        cannot widen anyone's authority, only keep a cancellation from demanding the
        reversal permission.
        """
        default = REVERSAL_AUTHORITY.get(reference_type, Permission.ACCOUNTS_MANAGE)
        if requested is None:
            return default
        allowed = REVERSAL_AUTHORITY_CHOICES.get(reference_type, frozenset({default}))
        if requested not in allowed:
            raise ValidationError(
                "That permission cannot authorise undoing this document.",
                details={
                    "reference_type": reference_type,
                    "requested_permission": str(requested),
                    "allowed_permissions": sorted(str(item) for item in allowed),
                },
            )
        return requested

    async def _authorize_permission(
        self,
        actor: ActorContext,
        *,
        permission: Permission,
        context: Mapping[str, Any],
    ) -> None:
        if str(permission) in actor.permissions:
            return
        await self._record_denial(actor, permission=permission, context=context)
        raise PermissionDeniedError(
            "You do not have permission to post to the ledger.",
            details={"required_permission": str(permission), **dict(context)},
        )

    async def _record_denial(
        self,
        actor: ActorContext,
        *,
        permission: Permission,
        context: Mapping[str, Any],
    ) -> None:
        """Persist a refused posting in its own transaction.

        The request that caused the refusal continues to a rollback (or never opened a
        transaction at all), so an audit row written in the caller's transaction would
        vanish with it — and an authorization denial that leaves no trace is exactly the
        kind of event an auditor looks for. This is the same pattern
        ``UserService._record_refusal`` uses for a refused privilege escalation.
        """
        async with self._database.transaction() as session:
            AuditService(session).record(
                action=AuditAction.LEDGER_POSTING_DENIED,
                entity_type="journal_entry",
                entity_id=None,
                new_data={
                    "required_permission": str(permission),
                    "roles": list(actor.roles),
                    "branch_id": str(actor.branch_id) if actor.branch_id else None,
                    **dict(context),
                },
                actor=actor,
            )

    async def _assert_scope(
        self,
        actor: ActorContext,
        *,
        branch_id: uuid.UUID | None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        """A posting may only touch the actor's own branch (unless global scope).

        The refusal is audited like a permission refusal: "who tried to post into another
        branch, and when" is a question with a financial answer, not a logging nicety.
        """
        if self._is_global(actor):
            return
        if actor.branch_id is None:
            await self._scope_refusal(
                actor,
                reason="ACTOR_HAS_NO_BRANCH",
                context={
                    "target_branch_id": str(branch_id) if branch_id else None,
                    **(context or {}),
                },
            )
        if branch_id != actor.branch_id:
            await self._scope_refusal(
                actor,
                reason="ANOTHER_BRANCH",
                context={
                    "target_branch_id": str(branch_id) if branch_id else None,
                    "actor_branch_id": str(actor.branch_id),
                    **(context or {}),
                },
            )

    async def _scope_refusal(
        self, actor: ActorContext, *, reason: str, context: Mapping[str, Any]
    ) -> NoReturn:
        async with self._database.transaction() as session:
            AuditService(session).record(
                action=AuditAction.LEDGER_POSTING_DENIED,
                entity_type="journal_entry",
                entity_id=None,
                new_data={"reason": reason, **dict(context)},
                actor=actor,
            )
        raise ForbiddenScopeError(
            "This branch is outside your scope.",
            details={"reason": reason, **dict(context)},
        )

    async def _read_scope(
        self, actor: ActorContext, *, branch_id: uuid.UUID | None
    ) -> list[uuid.UUID] | None:
        """The branch ids an actor may read (``None`` = every branch).

        Asking for a branch that is not yours is refused (403 ``FORBIDDEN_SCOPE``) rather
        than answered with an empty list: the two mean different things to a client, and
        a manager who filters by another branch should be told, not silently given
        nothing. An actor bound to no branch simply sees no rows — their scope is empty,
        which is not an error.
        """
        if self._is_global(actor):
            return [branch_id] if branch_id is not None else None
        if actor.branch_id is None:
            return []
        if branch_id is not None and branch_id != actor.branch_id:
            await self._scope_refusal(
                actor,
                reason="ANOTHER_BRANCH",
                context={
                    "target_branch_id": str(branch_id),
                    "actor_branch_id": str(actor.branch_id),
                },
            )
        return [actor.branch_id]

    def _can_see_branch(self, actor: ActorContext, branch_id: uuid.UUID | None) -> bool:
        """Whether one row's branch is inside the actor's scope (used by point reads)."""
        if self._is_global(actor):
            return True
        return branch_id is not None and branch_id == actor.branch_id

    @staticmethod
    def _is_global(actor: ActorContext) -> bool:
        return bool(set(actor.roles) & GROUP_WIDE_ROLES)


# ------------------------------------------------------------------- module helpers
logger = get_logger(__name__)

# A reversal is refused by ``ux_journal_entries_reversed_once`` when two attempts race:
# the pre-check in the service cannot see the other transaction's uncommitted row. The
# unique violation is therefore the *expected* outcome of a race and is named as such,
# instead of being reported as a generic duplicate.
_REVERSED_ONCE_CONSTRAINT = "ux_journal_entries_reversed_once"


def _ledger_refusal(exc: IntegrityError) -> NexusError | None:
    """The domain error for a refusal the database itself produced, if it is a known one."""
    sqlstate = sqlstate_of(exc)
    if sqlstate is None or sqlstate not in KNOWN_SQLSTATES:
        return None
    constraint = constraint_name_of(exc)
    details: dict[str, Any] = {"sqlstate": sqlstate}
    if constraint:
        details["constraint"] = constraint
    if sqlstate == "23505" and constraint == _REVERSED_ONCE_CONSTRAINT:
        return AlreadyReversedError(
            "This entry has already been reversed.",
            details=details,
        )
    return error_for_sqlstate(sqlstate, details=details)


def _validated_money(value: Decimal, *, field: str) -> Decimal:
    """A monetary value that ``NUMERIC(30,10)`` can store **exactly**.

    Refused with the API's validation error rather than ``MoneyError``: a value that does
    not fit the stored scale can only have come from outside (the service's own arithmetic
    rounds once per leg, with ``app.core.money``'s multiply/divide helpers), so it is a bad
    request and must not be
    reported as a server defect.
    """
    if not isinstance(value, Decimal):
        raise ValidationError(
            "A monetary value must be a Decimal.",
            details={"fields": [{"field": field, "code": "not_a_decimal"}]},
        )
    if not has_money_scale(value):
        raise ValidationError(
            "A monetary value cannot carry more than 10 decimal places.",
            details={"fields": [{"field": field, "code": "not_exact_scale"}]},
        )
    try:
        assert_within_money_bounds(value, field=field)
    except MoneyError as exc:
        raise ValidationError(
            "A monetary value exceeds the maximum the ledger can store.",
            details={"fields": [{"field": field, "code": "over_maximum"}]},
        ) from exc
    return value


def _positive(value: Decimal, *, field: str) -> Decimal:
    amount = _validated_money(value, field=field)
    if amount <= 0:
        raise ValidationError(
            "The value must be greater than zero.",
            details={"fields": [{"field": field, "code": "not_positive"}]},
        )
    return amount


def _non_negative(value: Decimal, *, field: str) -> Decimal:
    amount = _validated_money(value, field=field)
    if amount < 0:
        raise ValidationError(
            "The value may not be negative.",
            details={"fields": [{"field": field, "code": "negative"}]},
        )
    return amount


def _in_inventory_band(code: str) -> bool:
    """Whether an account code sits in the chart's cash-inventory band (§5).

    The seeded chart gives the base currency ``1000`` and every other currency the next
    code in the band, while ``1100`` *Cash in Transit* and ``1200`` *Customer Receivable*
    sit above it: two asset accounts bound to the same currency are only distinguishable
    by that convention, so the convention is stated once, here, instead of being guessed
    at each call site.
    """
    candidate = code.strip()
    return len(candidate) == 4 and candidate.isdigit() and 1000 <= int(candidate) <= 1099


def compute_exchange_amounts(
    *,
    transaction_type: str,
    from_amount: Decimal,
    exchange_rate: Decimal,
    commission: Decimal,
    from_decimal_places: int,
    to_decimal_places: int,
) -> ExchangeComputation:
    """The arithmetic of one exchange deal, validated (``ACCOUNTING_MODEL.md`` §6.2, §6.3).

    ``gross = from_amount x exchange_rate`` and the settlement is the *to*-side amount that
    actually moves: for a **BUY** the house keeps the commission out of the payout
    (``gross - commission``), for a **SELL** the customer pays the full receipt and the
    commission is recognized inside it (``gross``). The two cases differ in who ends up
    holding the fee, never in what is counted.

    Refusals are the ones a counter operator must be able to act on, and each names the
    field it belongs to: an amount or rate that is not positive, an amount finer than the
    currency's smallest unit, a rate so small it produces nothing, and a commission that is
    not smaller than the gross it is charged on (a fee that swallows the deal is not a fee,
    and the ledger would have to invent an entry to balance it).
    """
    if transaction_type not in EXCHANGE_TRANSACTION_TYPES:
        raise ValidationError(
            "An exchange is a BUY or a SELL.",
            details={
                "fields": [{"field": "transaction_type", "code": "unsupported"}],
                "transaction_type": transaction_type,
            },
        )
    amount_from = _positive(from_amount, field="from_amount")
    rate = _positive(exchange_rate, field="exchange_rate")
    fee = _non_negative(commission, field="commission")

    if amount_from != quantize_money(amount_from, from_decimal_places):
        raise ValidationError(
            "The amount cannot be expressed in the currency's smallest unit.",
            details={
                "fields": [{"field": "from_amount", "code": "below_smallest_unit"}],
                "decimal_places": from_decimal_places,
            },
        )

    gross = multiply_money(amount_from, rate, scale=to_decimal_places)
    if gross <= 0:
        raise ValidationError(
            "The applied rate produces no target amount to move.",
            details={"fields": [{"field": "exchange_rate", "code": "zero_result"}]},
        )
    if fee >= gross:
        raise ValidationError(
            "The commission is not smaller than the amount the customer pays.",
            details={
                "fields": [{"field": "commission", "code": "exceeds_gross"}],
                "gross_to_amount": format_decimal(gross),
                "commission": format_decimal(fee),
            },
        )
    settlement = (
        money_difference(gross, fee, scale=to_decimal_places)
        if transaction_type == "BUY"
        else gross
    )
    if settlement != quantize_money(settlement, to_decimal_places):
        raise ValidationError(
            "The settled amount cannot be expressed in the currency's smallest unit.",
            details={
                "fields": [{"field": "to_amount", "code": "below_smallest_unit"}],
                "decimal_places": to_decimal_places,
            },
        )
    return ExchangeComputation(
        transaction_type=transaction_type,
        from_amount=amount_from,
        exchange_rate=rate,
        commission=fee,
        gross_amount=gross,
        settlement_amount=settlement,
    )


def _assert_exchange_direction(
    *,
    transaction_type: str,
    from_currency: Currency,
    to_currency: Currency,
    base: Currency,
) -> None:
    """Refuse currency pairs that cannot describe an exchange (``ACCOUNTING_MODEL.md`` §6.2/§6.3).

    Three refusals, each naming the field it belongs to so an operator can fix the document
    rather than guess:

    * the same currency on both sides — nothing is exchanged;
    * the functional currency as the **delivered** side (``from_currency``) — a deal whose
      "foreign" leg is the money we measure everything in. A customer buying foreign
      currency from us is a SELL of that foreign currency (§6.3), not a BUY of the afghani;
    * therefore every accepted deal has a foreign ``from_currency`` and a ``to_currency``
      that is the functional currency or another foreign one.

    ``transaction_type`` is part of the error's context because the same pair can be valid
    for one direction and impossible for the other.
    """
    if from_currency.id == to_currency.id:
        raise ExchangeDirectionError(
            f"A {transaction_type} cannot exchange {from_currency.code} for itself.",
            details={
                "fields": [{"field": "to_currency_id", "code": "same_currency"}],
                "reason": "SAME_CURRENCY",
                "transaction_type": transaction_type,
                "from_currency_id": str(from_currency.id),
                "to_currency_id": str(to_currency.id),
            },
        )
    if from_currency.id == base.id:
        raise ExchangeDirectionError(
            f"A {transaction_type} cannot deliver the functional currency {base.code}.",
            details={
                "fields": [{"field": "from_currency_id", "code": "functional_currency"}],
                "reason": "FUNCTIONAL_CURRENCY_NOT_DELIVERABLE",
                "transaction_type": transaction_type,
                "functional_currency_code": base.code,
                "from_currency_id": str(from_currency.id),
                "to_currency_id": str(to_currency.id),
            },
        )


def _assert_cash_quantity(
    *, amount: Decimal, currency: Currency, field: str, transaction_type: str
) -> None:
    """A physical quantity below the currency's smallest unit cannot be paid out."""
    if amount != quantize_money(amount, currency.decimal_places):
        raise ValidationError(
            "The amount cannot be expressed in the currency's smallest unit.",
            details={
                "fields": [{"field": field, "code": "below_smallest_unit"}],
                "currency_code": currency.code,
                "decimal_places": currency.decimal_places,
                "transaction_type": transaction_type,
            },
        )


def _close_with_fx_result(
    lines: Sequence[PostingLine],
    *,
    fx_account_id: uuid.UUID | None,
    functional_currency_id: uuid.UUID,
    reference: str,
) -> list[PostingLine]:
    """Add the FX gain/loss line that makes the entry balance exactly.

    Returns immediately when the legs already balance — the documented cases, where one
    side is the functional currency and nothing is revalued. Otherwise the difference is
    posted to the FX account on the side that is short:

    * debits short of credits (``Σdebit < Σcredit``) → ``Dr 4000``: the business realized
      a **loss** (§6.3's worked example: ``gross`` received against ``cost`` relinquished,
      the gap being the commission and the rate result);
    * credits short (``Σdebit > Σcredit``) → ``Cr 4000``: a **gain**.

    A posting that needs the line but has no FX account is a configuration error, not a
    silent rounding: it is refused with the difference in the message.
    """
    debit = money_sum(line.debit for line in lines)
    credit = money_sum(line.credit for line in lines)
    difference = money_difference(debit, credit)
    if difference == 0:
        return list(lines)
    if fx_account_id is None:
        raise ValidationError(
            "This posting realizes an exchange result and needs the FX gain/loss account.",
            details={
                "fields": [{"field": "fx_account_id", "code": "required"}],
                "difference": format_decimal(difference),
                "reference": reference,
            },
        )
    gain = difference > 0
    amount = abs(difference)
    return [
        *lines,
        PostingLine(
            account_id=fx_account_id,
            currency_id=functional_currency_id,
            debit=Decimal(0) if gain else amount,
            credit=amount if gain else Decimal(0),
            exchange_rate=Decimal(1),
            description=f"FX {'gain' if gain else 'loss'} on {reference}",
        ),
    ]


def _reference_type(value: str) -> str:
    normalized = str(value).strip().upper()
    if normalized not in REFERENCE_TYPES:
        raise ValidationError(
            "That reference type is not a ledger reference.",
            details={
                "fields": [{"field": "reference_type", "code": "unsupported"}],
                "allowed": list(REFERENCE_TYPES),
            },
        )
    return normalized


def _transaction_type(value: str) -> str:
    normalized = str(value).strip().upper()
    if normalized not in EXCHANGE_TRANSACTION_TYPES:
        raise ValidationError(
            "A transaction is either a BUY or a SELL.",
            details={
                "fields": [{"field": "transaction_type", "code": "unsupported"}],
                "allowed": list(EXCHANGE_TRANSACTION_TYPES),
            },
        )
    return normalized


def _cash_movement_type(value: str) -> str:
    normalized = str(value).strip().upper()
    if normalized == "CLOSING":
        raise ValidationError(
            "A closing count is a reconciliation snapshot, not a journal entry.",
            details={
                "fields": [{"field": "movement_type", "code": "no_journal"}],
                "hint": "Post an ADJUSTMENT for the difference (Phase 6 cash module).",
            },
        )
    if normalized not in CASH_MOVEMENT_TYPES:
        raise ValidationError(
            "That cash movement type does not exist.",
            details={
                "fields": [{"field": "movement_type", "code": "unsupported"}],
                "allowed": list(CASH_MOVEMENT_TYPES),
            },
        )
    return normalized


def _adjustment_sign(movement_type: str, adjustment_sign: int | None) -> int:
    if movement_type != "ADJUSTMENT":
        if adjustment_sign is not None:
            raise ValidationError(
                "Only an adjustment carries a sign.",
                details={"fields": [{"field": "adjustment_sign", "code": "unexpected"}]},
            )
        return 0
    if adjustment_sign not in (-1, 1):
        raise ValidationError(
            "An adjustment needs a sign: +1 adds cash, -1 removes it.",
            details={"fields": [{"field": "adjustment_sign", "code": "required"}]},
        )
    return int(adjustment_sign)


def _accounting_date(value: dt.datetime | None, settings: Settings) -> dt.datetime:
    """The entry's accounting date, always UTC and never in the future.

    Back-dating is allowed (``ACCOUNTING_MODEL.md`` §11: the MVP has no period lock, so a
    late document must be postable for the day it belongs to), but post-dating is not —
    the system has no mechanism to know that tomorrow's trade will happen, and an entry
    dated in the future would land in a period whose totals a report may already have
    published. The tolerated skew (``BUSINESS_DATE_SKEW_MINUTES``) exists for clock drift
    between a field device and the server, not as a policy window.
    """
    if value is None:
        return dt.datetime.now(tz=dt.UTC)
    if value.tzinfo is None:
        raise ValidationError(
            "The accounting date must carry a timezone.",
            details={"fields": [{"field": "transaction_date", "code": "naive"}]},
        )
    moment = value.astimezone(dt.UTC)
    latest = dt.datetime.now(tz=dt.UTC) + dt.timedelta(minutes=settings.business_date_skew_minutes)
    if moment > latest:
        raise ValidationError(
            "The accounting date is in the future.",
            details={
                "fields": [{"field": "transaction_date", "code": "future_date"}],
                "latest_allowed": latest.isoformat(),
                "tolerated_skew_minutes": settings.business_date_skew_minutes,
            },
        )
    return moment


async def _require_base_currency(session: AsyncSession) -> Currency:
    """The ledger's functional currency, or a clear failure when the chart is broken."""
    base = await CurrencyRepository(session).base_currency()
    if base is None:  # pragma: no cover - the seed guarantees one
        raise DataIntegrityError(
            "The ledger has no functional currency.",
            details={"hint": "Exactly one currency must have is_base = true."},
        )
    return base


async def _require_currency(
    currencies: CurrencyRepository, currency_id: uuid.UUID, field: str
) -> Currency:
    currency = await currencies.get(currency_id)
    if currency is None:
        raise ResourceNotFoundError(
            "That currency does not exist.",
            details={"fields": [{"field": field, "code": "not_found"}]},
        )
    if not currency.is_active:
        raise CurrencyInactiveError(details={"fields": [{"field": field, "code": "inactive"}]})
    return currency


def _fingerprint_lines(lines: Sequence[PostingLine]) -> list[dict[str, str]]:
    """The posting lines as canonical data, for the idempotency fingerprint.

    Objects cannot be hashed: the fingerprint has to be plain values. Amounts are
    expressed at the ledger's own scale, so a client that retries ``70`` after sending
    ``70.00`` is asking for the same request (and would store the same number) rather than
    tripping the "key reused with a different request" refusal. Order matters: the lines
    are the entry, and swapping them changes what was asked for.
    """
    return [
        {
            "account_id": str(line.account_id),
            "currency_id": str(line.currency_id),
            "debit": _fingerprint_money(line.debit),
            "credit": _fingerprint_money(line.credit),
            "exchange_rate": _fingerprint_money(line.exchange_rate),
        }
        for line in lines
    ]


def _fingerprint_money(value: Any) -> str:
    """One money value as a fixed-point string, at the scale the ledger stores.

    Validation is what refuses a bad value (a float, a string, ``None``) and it must be
    allowed to say so: fingerprinting runs first, so anything that is not a ``Decimal`` is
    represented by its ``repr`` here and left for the validator to reject properly.
    """
    if isinstance(value, Decimal):
        return format_decimal(quantize_money(value))
    return repr(value)


def _fingerprint(**fields: Any) -> dict[str, Any]:
    """The caller's own inputs, canonicalised into an idempotency fingerprint.

    Only what the caller supplied belongs here. A ``transaction_date`` that the client
    omitted is filled with the server clock, and hashing that would turn a retry into a
    "key reused with a different request" (409) instead of a replay; the same goes for the
    device the request happened to arrive from. ``None`` values are dropped for the same
    reason: an absent optional argument and an explicitly null one are the same request.
    """
    return {name: value for name, value in fields.items() if value is not None}


def _entry_view(row: RowMapping, *, lines: tuple[JournalLineView, ...] = ()) -> JournalEntryView:
    return JournalEntryView(
        id=row["id"],
        reference_type=row["reference_type"],
        reference_id=row["reference_id"],
        description=row["description"],
        transaction_date=row["transaction_date"],
        created_at=row["created_at"],
        created_by=row["created_by"],
        created_by_username=row["created_by_username"],
        branch_id=row["branch_id"],
        branch_code=row["branch_code"],
        device_id=row["device_id"],
        reversal_of_id=row["reversal_of_id"],
        reversed_by_entry_id=row["reversed_by_entry_id"],
        line_count=int(row["line_count"]),
        total_debit=row["total_debit"],
        total_credit=row["total_credit"],
        lines=lines,
    )


def _line_view(row: RowMapping) -> JournalLineView:
    return JournalLineView(
        id=row["id"],
        account_id=row["account_id"],
        account_code=row["account_code"],
        account_name=row["account_name"],
        account_type=row["account_type"],
        currency_id=row["currency_id"],
        currency_code=row["currency_code"],
        debit=row["debit"],
        credit=row["credit"],
        exchange_rate=row["exchange_rate"],
        foreign_amount=row["foreign_amount"],
        description=row["description"],
    )


def build_accounting_service(*, database: Database, settings: Settings) -> AccountingService:
    """Construct the ledger writer for a request or a worker task."""
    return AccountingService(database=database, settings=settings)


__all__ = [
    "POSTING_AUTHORITY",
    "REFERENCE_TYPES",
    "REVERSAL_AUTHORITY",
    "AccountBalanceView",
    "AccountingService",
    "JournalEntryView",
    "JournalLineView",
    "PostingLine",
    "PostingTotals",
    "RateSnapshot",
    "TrialBalanceView",
    "build_accounting_service",
]
