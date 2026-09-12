"""Exchange engine: the business orchestration layer of an exchange deal (Phase 5).

This module owns the *document* — the deal a customer walked in with — and nothing else. It
is deliberately not a ledger: every financial line, every physical movement and every result
it stores comes from :class:`app.services.accounting_service.AccountingService`, inside one
database transaction that this service opens and the ledger posts into (PART 20, PART 46). A
deal this module cannot express through the ledger is a deal it refuses.

The order of operations is the contract of this file
------------------------------------------------------------------------
1. **authority** — permission (``exchange.create``) and branch scope, before anything is read;
2. **idempotency claim** — the ``Idempotency-Key`` is taken inside the caller's transaction,
   so a retry replays the recorded answer and a crash leaves no claim behind;
3. **offline replay** — a document that already exists for ``client_event_id`` is returned
   unchanged when the event is the same deal, and refused as a conflict when it is not
   (PART 34). This happens before any rate is resolved, so a replayed offline trade never
   depends on today's quotes;
4. **referential and direction validation** — branch, currencies, customer, device, and the
   BUY/SELL direction, which is checked *before* a rate is looked up (§5);
5. **rate resolution and snapshot** — one quote, resolved once, with its branch/instant
   provenance; the applied number is stored on the document and the snapshot travels to the
   ledger's audit trail, so a later publication can never re-price a posted deal (§6);
6. **computation** — ``compute_exchange_amounts`` (shared with the ledger, so the document
   and its entry cannot disagree) plus the optional client cross-check (§7, §8);
7. **chart resolution** — the branch's drawers for both currencies, the commission income
   account and the FX result account;
8. **document number** — ``next_document_number('NX', 'exchange_transaction', period)`` for
   the branch's own business day;
9. **document + journal + cash movements + audit** — written in that order, in one
   transaction; the document is ``PENDING`` until its posting exists and ``COMPLETED`` after.

Cancellation and reversal are the same story with a different ending. A cancellation posts
the ledger's mirror entry and mirrored movements while the document itself becomes
``CANCELLED``; a reversal creates a **mirror document** (the frozen ``NEX04`` trigger requires
it to mirror currencies and amounts), binds the ledger's mirror entry to it, and moves the
original to ``REVERSED``. Neither ever deletes or edits history (PART 22).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_actions import AuditAction
from app.core.config import Settings
from app.core.database import Database
from app.core.exceptions import (
    AlreadyReversedError,
    AmountMismatchError,
    ConflictError,
    CustomerInactiveError,
    DataIntegrityError,
    ForbiddenScopeError,
    InvalidStatusTransitionError,
    PermissionDeniedError,
    RateNotFoundError,
    RateOutOfToleranceError,
    ResourceNotFoundError,
    ReversalNotUndoableError,
    ValidationError,
)
from app.core.idempotency import (
    ENDPOINT_EXCHANGE_CANCEL,
    ENDPOINT_EXCHANGE_CREATE,
    ENDPOINT_EXCHANGE_REVERSE,
    IdempotencyGuard,
    IdempotencyRequest,
    canonical_request_hash,
)
from app.core.money import (
    divide_money,
    format_decimal,
    money_context,
    money_difference,
    money_sum,
)
from app.core.permissions import Permission
from app.models.account import Account
from app.models.branch import Branch
from app.models.customer import Customer
from app.models.exchange_transaction import ExchangeTransaction
from app.repositories.devices import DeviceRepository
from app.repositories.exchange import ExchangeTransactionRepository
from app.repositories.ledger_master import AccountRepository, ExchangeRateRepository
from app.repositories.masterdata import BranchRepository, CustomerRepository
from app.schemas.exchange import RECEIPT_VERSION
from app.services.accounting_service import (
    AccountingService,
    CashMovementSpec,
    RateSnapshot,
    build_accounting_service,
    compute_exchange_amounts,
)
from app.services.audit_service import ActorContext, AuditService

# The document vocabulary, exactly as the frozen CHECK constraints spell it.
REFERENCE_TYPE_EXCHANGE = "EXCHANGE_TRANSACTION"
REFERENCE_TYPE_REVERSAL = "REVERSAL"
STATUS_PENDING = "PENDING"
STATUS_COMPLETED = "COMPLETED"
STATUS_CANCELLED = "CANCELLED"
STATUS_REVERSED = "REVERSED"
ORIGIN_ONLINE = "ONLINE"
ORIGIN_OFFLINE = "OFFLINE"

# The chart accounts an exchange needs beyond the drawers (``ACCOUNTING_MODEL.md`` §5):
# 4000 is where the realized FX result lands, 4010 is the commission the house keeps.
# Resolved by code because the accounting model *names* them; a chart without them is a
# misconfigured installation, and the refusal says which code is missing.
COMMISSION_ACCOUNT_CODE = "4010"
FX_RESULT_ACCOUNT_CODE = "4000"

# The two physical movement types this engine writes, and their mirrors.
_MOVEMENT_FLIP = {"IN": "OUT", "OUT": "IN"}


@dataclass(frozen=True, slots=True)
class ExchangeRequest:
    """One deal as the service receives it, already parsed and float-free."""

    transaction_type: str
    branch_id: uuid.UUID
    from_currency_id: uuid.UUID
    to_currency_id: uuid.UUID
    from_amount: Decimal
    exchange_rate: Decimal
    commission: Decimal = Decimal(0)
    to_amount: Decimal | None = None
    customer_id: uuid.UUID | None = None
    device_id: uuid.UUID | None = None
    client_event_id: uuid.UUID | None = None
    transaction_date: dt.datetime | None = None
    description: str | None = None

    def fingerprint(self) -> dict[str, Any]:
        """The economic identity of the request, for idempotency and offline replay.

        What the operator decided — the deal's sides, its amounts, its rate, its fee, its
        counterparty — and not how a retry travelled: ``device_id`` and ``transaction_date``
        are transport and defaulted metadata. The client's optional ``to_amount`` expectation
        *is* part of the request: a retry claiming a different settlement is not the same
        request, and answering it with the recorded result would hide the disagreement.
        """
        return {
            "transaction_type": self.transaction_type,
            "branch_id": self.branch_id,
            "customer_id": self.customer_id,
            "from_currency_id": self.from_currency_id,
            "from_amount": self.from_amount,
            "to_currency_id": self.to_currency_id,
            "to_amount": self.to_amount,
            "exchange_rate": self.exchange_rate,
            "commission": self.commission,
            "client_event_id": self.client_event_id,
        }


@dataclass(frozen=True, slots=True)
class ExchangeResult:
    """The answer to a create/cancel/reverse call.

    ``payload`` is the exact JSON body the caller receives — and the exact body recorded
    against the ``Idempotency-Key``, which is what makes a replay byte-identical instead of
    re-rendered. ``replayed`` tells the route (and the tests) which of the two happened.
    """

    payload: dict[str, Any]
    status_code: int
    replayed: bool = False
    transaction_id: uuid.UUID | None = None
    transaction_number: str | None = None


@dataclass(frozen=True, slots=True)
class ExchangeView:
    """One exchange document, with everything a reader needs to verify it."""

    row: Mapping[str, Any]
    cash_movements: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    # ------------------------------------------------------------------ helpers
    @property
    def id(self) -> uuid.UUID:
        return uuid.UUID(str(self.row["id"]))

    @property
    def transaction_number(self) -> str:
        return str(self.row["transaction_number"])

    @property
    def status(self) -> str:
        return str(self.row["status"])

    @property
    def transaction_type(self) -> str:
        return str(self.row["transaction_type"])

    @property
    def branch_id(self) -> uuid.UUID:
        return uuid.UUID(str(self.row["branch_id"]))

    @property
    def journal_entry_id(self) -> uuid.UUID | None:
        value = self.row.get("journal_entry_id")
        return uuid.UUID(str(value)) if value else None

    @property
    def gross_amount(self) -> Decimal:
        """The to-side amount before the commission, rebuilt from persisted columns.

        A ``BUY`` withholds the fee from the payout, so the gross is the settlement plus the
        fee; a ``SELL`` collects it inside the receipt, so the settlement *is* the gross
        (``ACCOUNTING_MODEL.md`` §6.2, §6.3). Rebuilding it this way — instead of multiplying
        the amount by the rate again — cannot drift from what was posted, and it is the number
        a receipt has to show beside the fee.
        """
        settlement = Decimal(str(self.row["to_amount"]))
        fee = Decimal(str(self.row["commission"]))
        if self.transaction_type == "BUY":
            return money_sum((settlement, fee))
        return settlement

    @property
    def business_date(self) -> dt.date:
        return business_date(
            _as_utc(self.row["created_at"]),
            timezone_name=_optional_str(self.row.get("branch_timezone")),
        )

    @property
    def reversal_reason(self) -> str | None:
        """Why the document was undone, from wherever the frozen schema keeps it.

        A reversal stores its reason on the *mirror* row (the CHECK constraint reserves
        ``reversal_reason`` for a row that points at another document, which the original does
        not), and a cancellation has no mirror row at all, so its reason is the description of
        the reversal entry the ledger posted. Both are committed facts; the payload reports
        them rather than leaving a reader to join tables by hand.
        """
        own = _optional_str(self.row.get("reversal_reason"))
        if own:
            return own
        if self.status == STATUS_REVERSED:
            return _optional_str(self.row.get("reversal_document_reason"))
        if self.status == STATUS_CANCELLED:
            return _optional_str(self.row.get("reversal_entry_description"))
        return None

    def to_payload(self) -> dict[str, Any]:
        """The wire form of the document (``app.schemas.exchange.ExchangeDocument``)."""
        row = self.row
        return {
            "id": str(self.id),
            "transaction_number": self.transaction_number,
            "status": self.status,
            "transaction_type": self.transaction_type,
            "origin": str(row["origin"]),
            "branch_id": str(self.branch_id),
            "branch_code": str(row["branch_code"]),
            "branch_name": str(row["branch_name"]),
            "cashier_id": str(row["cashier_id"]),
            "cashier_username": str(row["cashier_username"]),
            "customer_id": _optional_uuid(row.get("customer_id")),
            "customer_code": _optional_str(row.get("customer_code")),
            "customer_name": _optional_str(row.get("customer_name")),
            "device_id": _optional_uuid(row.get("device_id")),
            "cash_session_id": _optional_uuid(row.get("cash_session_id")),
            "client_event_id": _optional_uuid(row.get("client_event_id")),
            "from_currency_id": str(row["from_currency_id"]),
            "from_currency_code": str(row["from_currency_code"]),
            "from_amount": _money(row["from_amount"]),
            "to_currency_id": str(row["to_currency_id"]),
            "to_currency_code": str(row["to_currency_code"]),
            "to_amount": _money(row["to_amount"]),
            "exchange_rate": _money(row["exchange_rate"]),
            "commission": _money(row["commission"]),
            "gross_amount": _money(self.gross_amount),
            "journal_entry_id": _optional_uuid(row.get("journal_entry_id")),
            "journal_total_debit": _optional_money(row.get("journal_total_debit")),
            "journal_total_credit": _optional_money(row.get("journal_total_credit")),
            "reversal_of_id": _optional_uuid(row.get("reversal_of_id")),
            "reversal_transaction_id": _optional_uuid(row.get("reversal_transaction_id")),
            "reversal_transaction_number": _optional_str(row.get("reversal_transaction_number")),
            "reversal_journal_entry_id": _optional_uuid(row.get("reversal_journal_entry_id")),
            "reversal_reason": self.reversal_reason,
            "reversed_by": _optional_uuid(row.get("reversed_by")),
            "reversed_at": _iso(row.get("reversed_at")),
            "version": int(row["version"]),
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
            "cash_movements": [self._movement_payload(item) for item in self.cash_movements],
            "receipt": {"url": f"/api/v1/exchange/{self.id}/receipt"},
        }

    @staticmethod
    def _movement_payload(movement: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": str(movement["id"]),
            "movement_type": str(movement["movement_type"]),
            "amount": _money(movement["amount"]),
            "currency_id": str(movement["currency_id"]),
            "currency_code": str(movement["currency_code"]),
            "account_id": str(movement["account_id"]),
            "account_code": str(movement["account_code"]),
            "signed_amount": _money(movement["signed_amount"]),
            "journal_entry_id": _optional_uuid(movement.get("journal_entry_id")),
            "created_at": _iso(movement["created_at"]),
            "reference_type": _optional_str(movement.get("reference_type")),
        }


# ------------------------------------------------------------------ value helpers
def _as_utc(value: Any) -> dt.datetime:
    moment = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def _iso(value: Any) -> Any:
    return _as_utc(value).isoformat() if value is not None else None


def _money(value: Any) -> str:
    return format_decimal(Decimal(str(value)))


def _optional_money(value: Any) -> str | None:
    return None if value is None else _money(value)


def _receipt_lines(items: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """The customer-facing view of a group of movements: type, currency, amount."""
    return [
        {
            "movement_type": str(item["movement_type"]),
            "currency_code": str(item["currency_code"]),
            "amount": _money(item["amount"]),
        }
        for item in items
    ]


def _optional_uuid(value: Any) -> str | None:
    return str(value) if value else None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def business_date(
    moment: dt.datetime, *, timezone_name: str | None, fallback: str = "UTC"
) -> dt.date:
    """The branch's business date: the day the operator would call "today" at the counter.

    Document numbers roll over on the *branch's* day, not on UTC's — the counter's midnight is
    what the customer sees on the paper — which is why the branch carries a timezone at all.
    A stored timezone the tz database does not know is a data defect, not a reason to number
    documents in a different day silently.
    """
    name = timezone_name or fallback
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - tzdata present
        raise DataIntegrityError(
            "That branch has an unusable timezone, so its business date is unknown.",
            details={"branch_timezone": name, "hint": "Set a valid IANA timezone on the branch."},
        ) from exc
    return moment.astimezone(zone).date()


def _period_of(day: dt.date) -> str:
    return day.strftime("%Y%m%d")


class ExchangeService:
    """The exchange document service: validation, orchestration, audit (§1-§19)."""

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        accounting: AccountingService | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        # One ledger instance per service: the accounting engine is stateless beyond its
        # session factory and settings, and injecting it keeps the "one writer" rule visible
        # in the constructor instead of buried in a call.
        self._accounting = accounting or build_accounting_service(
            database=database, settings=settings
        )

    @property
    def accounting(self) -> AccountingService:
        """The ledger this engine posts through (PART 46: there is exactly one).

        Exposed so a caller that already holds the exchange engine — a test scaffolding a
        funded drawer, a later phase posting the cash leg of its own document — uses the same
        instance, and therefore the same transaction manager, instead of building a second
        path into the ledger.
        """
        return self._accounting

    # =================================================================== creating
    async def create_exchange(
        self,
        *,
        actor: ActorContext,
        request: ExchangeRequest,
        idempotency_key: uuid.UUID,
    ) -> ExchangeResult:
        """Record one deal: document, journal, cash movements and audit, atomically."""
        fingerprint = request.fingerprint()
        async with self._accounting.document_transaction() as session:
            await self._authorize(
                actor,
                Permission.EXCHANGE_CREATE,
                context={
                    "branch_id": str(request.branch_id),
                    "transaction_type": request.transaction_type,
                },
            )
            await self._accounting.assert_branch_scope(
                actor,
                branch_id=request.branch_id,
                context={"operation": "exchange.create"},
            )
            guard = await self._claim(
                session,
                actor=actor,
                key=idempotency_key,
                endpoint=ENDPOINT_EXCHANGE_CREATE,
                fingerprint=fingerprint,
            )
            if isinstance(guard, ExchangeResult):
                return guard

            # An offline event that was already recorded is answered from what is stored,
            # before any rate, currency or customer is looked at again: a replayed trade must
            # not become impossible because the catalogue moved on.
            replay = await self._replay_client_event(session, actor=actor, request=request)
            if replay is not None:
                return replay

            branch = await self._require_branch(session, request.branch_id)
            from_currency, to_currency, _base = await self._accounting.resolve_exchange_currencies(
                session,
                transaction_type=request.transaction_type,
                from_currency_id=request.from_currency_id,
                to_currency_id=request.to_currency_id,
            )
            device_id = await self._resolve_device(
                session, actor=actor, requested=request.device_id, branch_id=request.branch_id
            )
            customer = await self._resolve_customer(
                session, customer_id=request.customer_id, branch_id=request.branch_id
            )
            moment = self._accounting.accounting_date(request.transaction_date)
            # The deal's own arithmetic is checked *before* the quote is looked up: a
            # non-positive amount or rate, a negative commission or a commission that swallows
            # the gross is a malformed request, and answering it with a rate diagnosis
            # (``RATE_OUT_OF_TOLERANCE``/``RATE_NOT_FOUND``) would send the counter chasing the
            # wrong problem. The numbers are still the ones the client stated — the quote
            # constrains the rate, it never silently replaces it (§6).
            computation = compute_exchange_amounts(
                transaction_type=request.transaction_type,
                from_amount=request.from_amount,
                exchange_rate=request.exchange_rate,
                commission=request.commission,
                from_decimal_places=from_currency.decimal_places,
                to_decimal_places=to_currency.decimal_places,
            )
            self._assert_client_amount(
                stated=request.to_amount,
                computed=computation.settlement_amount,
                decimal_places=to_currency.decimal_places,
                currency_code=to_currency.code,
            )
            quote = await self._resolve_quote(
                session, request=request, moment=moment, branch_id=request.branch_id
            )
            from_account = await self._accounting.inventory_account(
                session, branch_id=request.branch_id, currency_id=from_currency.id
            )
            to_account = await self._accounting.inventory_account(
                session, branch_id=request.branch_id, currency_id=to_currency.id
            )
            commission_account = await self._require_chart_account(
                session, code=COMMISSION_ACCOUNT_CODE, purpose="commission income"
            )
            fx_account = await self._require_chart_account(
                session, code=FX_RESULT_ACCOUNT_CODE, purpose="realized FX result"
            )
            repository = ExchangeTransactionRepository(session)
            cash_session_id = await repository.open_cash_session_id(
                branch_id=request.branch_id, device_id=device_id
            )
            number = await repository.next_transaction_number(
                period=_period_of(business_date(moment, timezone_name=branch.timezone))
            )
            document = ExchangeTransaction(
                transaction_number=number,
                branch_id=request.branch_id,
                device_id=device_id,
                cashier_id=actor.user_id,
                customer_id=customer.id if customer is not None else None,
                transaction_type=request.transaction_type,
                from_currency_id=from_currency.id,
                from_amount=computation.from_amount,
                to_currency_id=to_currency.id,
                to_amount=computation.settlement_amount,
                exchange_rate=computation.exchange_rate,
                commission=computation.commission,
                status=STATUS_PENDING,
                origin=ORIGIN_OFFLINE if request.client_event_id else ORIGIN_ONLINE,
                client_event_id=request.client_event_id,
                cash_session_id=cash_session_id,
            )
            repository.add(document)
            await repository.flush()

            entry = await self._accounting.post_exchange(
                session=session,
                transaction_type=request.transaction_type,
                reference_id=document.id,
                branch_id=request.branch_id,
                from_currency_id=from_currency.id,
                to_currency_id=to_currency.id,
                from_amount=computation.from_amount,
                exchange_rate=computation.exchange_rate,
                from_cash_account_id=from_account.id,
                to_cash_account_id=to_account.id,
                fx_account_id=fx_account.id,
                commission=computation.commission,
                commission_account_id=commission_account.id,
                description=request.description,
                transaction_date=moment,
                device_id=device_id,
                rate_snapshot=quote,
                actor=actor,
            )
            await self._accounting.record_cash_movements(
                session,
                reference_type=REFERENCE_TYPE_EXCHANGE,
                reference_id=document.id,
                branch_id=request.branch_id,
                movements=self._settlement_movements(
                    transaction_type=request.transaction_type,
                    from_amount=computation.from_amount,
                    to_amount=computation.settlement_amount,
                    from_account_id=from_account.id,
                    from_currency_id=from_currency.id,
                    to_account_id=to_account.id,
                    to_currency_id=to_currency.id,
                    label=number,
                ),
                actor=actor,
                journal_entry_id=entry.id,
                cash_session_id=cash_session_id,
                device_id=device_id,
            )
            document.status = STATUS_COMPLETED
            document.journal_entry_id = entry.id
            await repository.flush()

            AuditService(session).record(
                action=AuditAction.EXCHANGE_CREATED,
                entity_type="exchange_transaction",
                entity_id=document.id,
                new_data={
                    "transaction_number": number,
                    "transaction_type": request.transaction_type,
                    "origin": document.origin,
                    "branch_id": str(request.branch_id),
                    "customer_id": str(customer.id) if customer is not None else None,
                    "device_id": str(device_id) if device_id else None,
                    "cash_session_id": str(cash_session_id) if cash_session_id else None,
                    "from_currency": from_currency.code,
                    "from_amount": format_decimal(computation.from_amount),
                    "to_currency": to_currency.code,
                    "to_amount": format_decimal(computation.settlement_amount),
                    "gross_amount": format_decimal(computation.gross_amount),
                    "exchange_rate": format_decimal(computation.exchange_rate),
                    "commission": format_decimal(computation.commission),
                    "rate_snapshot": quote.as_payload(),
                    "journal_entry_id": str(entry.id),
                    "journal_total_debit": format_decimal(entry.total_debit),
                    "journal_total_credit": format_decimal(entry.total_credit),
                    "from_cash_account_id": str(from_account.id),
                    "to_cash_account_id": str(to_account.id),
                    "client_event_id": (
                        str(request.client_event_id) if request.client_event_id else None
                    ),
                },
                actor=actor,
            )

            view = await self._load_view(session, document.id)
            payload = view.to_payload()
            assert guard is not None  # a replay returned above
            guard.complete(
                status_code=201,
                body=payload,
                resource_type="exchange_transaction",
                resource_id=document.id,
            )
            return ExchangeResult(
                payload=payload,
                status_code=201,
                transaction_id=document.id,
                transaction_number=number,
            )

    # ================================================================== lifecycle
    async def cancel_exchange(
        self,
        *,
        actor: ActorContext,
        transaction_id: uuid.UUID,
        reason: str,
        idempotency_key: uuid.UUID,
        transaction_date: dt.datetime | None = None,
    ) -> ExchangeResult:
        """Cancel a completed deal: the ledger posts a mirror entry, nothing is deleted."""
        async with self._accounting.document_transaction() as session:
            await self._authorize(
                actor, Permission.EXCHANGE_CANCEL, context={"transaction_id": str(transaction_id)}
            )
            guard = await self._claim(
                session,
                actor=actor,
                key=idempotency_key,
                endpoint=ENDPOINT_EXCHANGE_CANCEL,
                fingerprint={"transaction_id": transaction_id, "reason": reason},
            )
            if isinstance(guard, ExchangeResult):
                return guard

            document = await self._lock_document(
                session, actor=actor, transaction_id=transaction_id
            )
            self._require_status(document, allowed=(STATUS_COMPLETED,), operation="cancel")
            self._assert_not_a_reversal(document, operation="cancel")
            if document.journal_entry_id is None:  # pragma: no cover - a bypassed posting
                raise DataIntegrityError(
                    "This document has no journal entry to reverse.",
                    details={"transaction_id": str(document.id), "reason": "JOURNAL_MISSING"},
                )
            moment = self._accounting.accounting_date(transaction_date)
            entry = await self._accounting.reverse_transaction(
                reference_type=REFERENCE_TYPE_EXCHANGE,
                reference_id=document.id,
                reason=reason,
                actor=actor,
                session=session,
                transaction_date=moment,
                device_id=document.device_id,
                authority=Permission.EXCHANGE_CANCEL,
            )
            await self._record_reversing_movements(
                session,
                document=document,
                actor=actor,
                journal_entry_id=entry.id,
                reference_type=REFERENCE_TYPE_REVERSAL,
                reference_id=document.id,
                label=document.transaction_number,
            )
            # The frozen CHECK reserves ``reversal_reason`` for a row that points at another
            # document, which a cancelled document does not: its reason lives where it is
            # observable — as the reversal entry's description (the ledger writes it) and in
            # the audit row below.
            document.status = STATUS_CANCELLED
            document.reversal_journal_entry_id = entry.id
            await self._flush(session)

            AuditService(session).record(
                action=AuditAction.EXCHANGE_CANCELLED,
                entity_type="exchange_transaction",
                entity_id=document.id,
                new_data={
                    "transaction_number": document.transaction_number,
                    "reason": reason,
                    "reversal_journal_entry_id": str(entry.id),
                    "journal_total_debit": format_decimal(entry.total_debit),
                    "journal_total_credit": format_decimal(entry.total_credit),
                    "branch_id": str(document.branch_id),
                },
                actor=actor,
            )
            view = await self._load_view(session, document.id)
            payload = view.to_payload()
            assert guard is not None
            guard.complete(
                status_code=200,
                body=payload,
                resource_type="exchange_transaction",
                resource_id=document.id,
            )
            return ExchangeResult(
                payload=payload,
                status_code=200,
                transaction_id=document.id,
                transaction_number=document.transaction_number,
            )

    async def reverse_exchange(
        self,
        *,
        actor: ActorContext,
        transaction_id: uuid.UUID,
        reason: str,
        idempotency_key: uuid.UUID,
        transaction_date: dt.datetime | None = None,
    ) -> ExchangeResult:
        """Reverse a completed deal: a mirror document, its entry and its cash movements."""
        async with self._accounting.document_transaction() as session:
            await self._authorize(
                actor, Permission.EXCHANGE_REVERSE, context={"transaction_id": str(transaction_id)}
            )
            guard = await self._claim(
                session,
                actor=actor,
                key=idempotency_key,
                endpoint=ENDPOINT_EXCHANGE_REVERSE,
                fingerprint={"transaction_id": transaction_id, "reason": reason},
            )
            if isinstance(guard, ExchangeResult):
                return guard

            document = await self._lock_document(
                session, actor=actor, transaction_id=transaction_id
            )
            if document.status == STATUS_REVERSED:
                raise AlreadyReversedError(
                    "This document has already been reversed.",
                    details={"transaction_id": str(document.id), "status": document.status},
                )
            self._require_status(document, allowed=(STATUS_COMPLETED,), operation="reverse")
            self._assert_not_a_reversal(document, operation="reverse")
            repository = ExchangeTransactionRepository(session)
            if await repository.reversal_of(document.id) is not None:
                raise AlreadyReversedError(
                    "This document already has a reversing document.",
                    details={"transaction_id": str(document.id)},
                )
            if document.journal_entry_id is None:  # pragma: no cover - a bypassed posting
                raise DataIntegrityError(
                    "This document has no journal entry to reverse.",
                    details={"transaction_id": str(document.id), "reason": "JOURNAL_MISSING"},
                )

            moment = self._accounting.accounting_date(transaction_date)
            entry = await self._accounting.reverse_journal_entry(
                journal_entry_id=document.journal_entry_id,
                reason=reason,
                actor=actor,
                transaction_date=moment,
                device_id=document.device_id,
                session=session,
                authority=Permission.EXCHANGE_REVERSE,
            )
            from_account = await self._accounting.inventory_account(
                session, branch_id=document.branch_id, currency_id=document.to_currency_id
            )
            to_account = await self._accounting.inventory_account(
                session, branch_id=document.branch_id, currency_id=document.from_currency_id
            )
            # ``NEX04`` fixes the mirror: same branch and type, the two currencies swapped and
            # each amount carried to the other side. The mirror's rate is its own effective
            # rate (original from / original to), quantized once — the exact quantities are the
            # columns, and the authoritative undo is the ledger's mirror entry, whose lines
            # reverse the original's to the last decimal.
            mirror_rate = divide_money(document.from_amount, document.to_amount)
            timezone_name = await self._branch_timezone(session, document.branch_id)
            number = await repository.next_transaction_number(
                period=_period_of(business_date(moment, timezone_name=timezone_name))
            )
            mirror = ExchangeTransaction(
                transaction_number=number,
                branch_id=document.branch_id,
                device_id=document.device_id,
                cashier_id=actor.user_id,
                customer_id=document.customer_id,
                transaction_type=document.transaction_type,
                from_currency_id=document.to_currency_id,
                from_amount=document.to_amount,
                to_currency_id=document.from_currency_id,
                to_amount=document.from_amount,
                exchange_rate=mirror_rate,
                commission=Decimal(0),
                status=STATUS_PENDING,
                origin=ORIGIN_ONLINE,
                cash_session_id=document.cash_session_id,
                reversal_of_id=document.id,
                reversal_reason=reason,
                journal_entry_id=entry.id,
            )
            repository.add(mirror)
            await repository.flush()
            await self._accounting.record_cash_movements(
                session,
                reference_type=REFERENCE_TYPE_EXCHANGE,
                reference_id=mirror.id,
                branch_id=document.branch_id,
                movements=self._settlement_movements(
                    transaction_type=document.transaction_type,
                    from_amount=mirror.from_amount,
                    to_amount=mirror.to_amount,
                    from_account_id=from_account.id,
                    from_currency_id=mirror.from_currency_id,
                    to_account_id=to_account.id,
                    to_currency_id=mirror.to_currency_id,
                    label=number,
                ),
                actor=actor,
                journal_entry_id=entry.id,
                cash_session_id=document.cash_session_id,
                device_id=document.device_id,
                allow_inactive_branch=True,
            )
            mirror.status = STATUS_COMPLETED

            document.status = STATUS_REVERSED
            document.reversed_at = moment
            document.reversed_by = actor.user_id
            document.reversal_journal_entry_id = entry.id
            await self._flush(session)

            # The reversing document is a document in its own right (it owns a number, an
            # entry and a movement pair), so it gets its own creation record: a reader who
            # looks up the mirror's history must find who wrote it, why, and what it undoes,
            # without having to guess that the original's audit row describes it too.
            AuditService(session).record(
                action=AuditAction.EXCHANGE_CREATED,
                entity_type="exchange_transaction",
                entity_id=mirror.id,
                new_data={
                    "role": "REVERSAL_DOCUMENT",
                    "transaction_number": number,
                    "transaction_type": document.transaction_type,
                    "origin": mirror.origin,
                    "branch_id": str(document.branch_id),
                    "customer_id": str(document.customer_id) if document.customer_id else None,
                    "device_id": str(document.device_id) if document.device_id else None,
                    "reversal_of_id": str(document.id),
                    "reverses_transaction_number": document.transaction_number,
                    "reversal_reason": reason,
                    "from_currency": str(mirror.from_currency_id),
                    "from_amount": format_decimal(mirror.from_amount),
                    "to_currency": str(mirror.to_currency_id),
                    "to_amount": format_decimal(mirror.to_amount),
                    "exchange_rate": format_decimal(mirror.exchange_rate),
                    "commission": format_decimal(mirror.commission),
                    "journal_entry_id": str(entry.id),
                    "journal_total_debit": format_decimal(entry.total_debit),
                    "journal_total_credit": format_decimal(entry.total_credit),
                },
                actor=actor,
            )
            AuditService(session).record(
                action=AuditAction.EXCHANGE_REVERSED,
                entity_type="exchange_transaction",
                entity_id=document.id,
                new_data={
                    "transaction_number": document.transaction_number,
                    "reason": reason,
                    "reversal_transaction_id": str(mirror.id),
                    "reversal_transaction_number": number,
                    "reversal_journal_entry_id": str(entry.id),
                    "journal_total_debit": format_decimal(entry.total_debit),
                    "journal_total_credit": format_decimal(entry.total_credit),
                    "branch_id": str(document.branch_id),
                    "reversed_at": moment.isoformat(),
                },
                actor=actor,
            )
            view = await self._load_view(session, document.id)
            payload = view.to_payload()
            assert guard is not None
            guard.complete(
                status_code=200,
                body=payload,
                resource_type="exchange_transaction",
                resource_id=document.id,
            )
            return ExchangeResult(
                payload=payload,
                status_code=200,
                transaction_id=document.id,
                transaction_number=document.transaction_number,
            )

    # =================================================================== reading
    async def get_exchange(self, *, actor: ActorContext, transaction_id: uuid.UUID) -> ExchangeView:
        """One document, or ``404`` when it is outside the caller's scope.

        Out of scope answers "does not exist" rather than "not yours": telling a cashier that
        another branch's document exists is itself a disclosure (the Phase 4 read rule).
        """
        async with self._database.session() as session:
            view = await self._load_view(session, transaction_id)
            if not self._accounting.branch_in_scope(actor, view.branch_id):
                raise ResourceNotFoundError(
                    "That exchange transaction does not exist.",
                    details={"resource": "exchange_transaction", "id": str(transaction_id)},
                )
            return view

    async def list_exchanges(
        self,
        *,
        actor: ActorContext,
        branch_id: uuid.UUID | None = None,
        cashier_id: uuid.UUID | None = None,
        customer_id: uuid.UUID | None = None,
        transaction_type: str | None = None,
        status: str | None = None,
        number_query: str | None = None,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ExchangeView], int]:
        """One page of the exchange book, scoped to the caller's branches."""
        scope = await self._accounting.branch_scope_filter(actor, branch_id=branch_id)
        async with self._database.session() as session:
            repository = ExchangeTransactionRepository(session)
            rows, total = await repository.list_exchanges(
                branch_ids=scope,
                cashier_id=cashier_id,
                customer_id=customer_id,
                transaction_type=transaction_type,
                status=status,
                number_query=number_query,
                from_=from_,
                to=to,
                limit=limit,
                offset=offset,
            )
            movements = await repository.cash_movements_by_document(
                [uuid.UUID(str(row["id"])) for row in rows]
            )
            views = [
                ExchangeView(
                    row=row,
                    cash_movements=tuple(movements.get(uuid.UUID(str(row["id"])), [])),
                )
                for row in rows
            ]
            return views, total

    async def build_receipt(
        self, *, actor: ActorContext, transaction_id: uuid.UUID
    ) -> dict[str, Any]:
        """The deterministic receipt payload of one document (API_CONTRACT §9.3).

        Every field is read from committed rows: no clock, no "current" rate, no generated
        reference. Rendering the same document twice therefore produces the same payload, and
        a receipt printed today still shows the rate that was applied — which is the whole
        point of storing the applied rate on the document instead of re-resolving it (§6).
        """
        view = await self.get_exchange(actor=actor, transaction_id=transaction_id)
        row = view.row
        return {
            "receipt_version": RECEIPT_VERSION,
            "transaction_number": view.transaction_number,
            "status": view.status,
            "transaction_type": view.transaction_type,
            "issued_at": _iso(row["created_at"]),
            "business_date": view.business_date.isoformat(),
            "branch_code": str(row["branch_code"]),
            "branch_name": str(row["branch_name"]),
            "branch_address": _optional_str(row.get("branch_address")),
            "branch_phone": _optional_str(row.get("branch_phone")),
            "cashier_username": str(row["cashier_username"]),
            "customer_code": _optional_str(row.get("customer_code")),
            "customer_name": _optional_str(row.get("customer_name")),
            "from_currency_code": str(row["from_currency_code"]),
            "from_amount": _money(row["from_amount"]),
            "to_currency_code": str(row["to_currency_code"]),
            "to_amount": _money(row["to_amount"]),
            "exchange_rate": _money(row["exchange_rate"]),
            "gross_amount": _money(view.gross_amount),
            "commission": _money(row["commission"]),
            # Two settlement lists, never one blended one: the money this document moved, and
            # the money its undo moved back. A receipt that showed four lines for a cancelled
            # deal would leave the reader to guess which pair was which.
            "settlement": _receipt_lines(
                item
                for item in view.cash_movements
                if item["reference_type"] == REFERENCE_TYPE_EXCHANGE
            ),
            "reversal_settlement": _receipt_lines(
                item
                for item in view.cash_movements
                if item["reference_type"] == REFERENCE_TYPE_REVERSAL
            ),
            "journal_entry_id": _optional_uuid(row.get("journal_entry_id")),
            "reversal_of_id": _optional_uuid(row.get("reversal_of_id")),
            "reversal_transaction_number": _optional_str(row.get("reversal_transaction_number")),
            "origin": str(row["origin"]),
            "document_id": str(view.id),
        }

    # ================================================================= internals
    async def _authorize(
        self, actor: ActorContext, permission: Permission, *, context: Mapping[str, Any]
    ) -> None:
        """The service-level permission check, with the refusal written to the audit trail.

        The route already enforces the permission (deny by default, PART 41); this is the
        second lock on the same door, and the one that leaves a trace — a document service is
        callable from a worker or a later phase, where no route checked anything. The refusal
        is recorded in its own transaction so it survives this request's rollback.
        """
        if str(permission) in actor.permissions:
            return
        async with self._database.transaction() as session:
            AuditService(session).record(
                action=AuditAction.EXCHANGE_ACCESS_DENIED,
                entity_type="exchange_transaction",
                entity_id=None,
                new_data={
                    "reason": "PERMISSION_DENIED",
                    "required_permission": str(permission),
                    "roles": list(actor.roles),
                    **dict(context),
                },
                actor=actor,
            )
        raise PermissionDeniedError(
            "You do not have permission to perform this action.",
            details={"required_permission": str(permission), **dict(context)},
        )

    async def _claim(
        self,
        session: AsyncSession,
        *,
        actor: ActorContext,
        key: uuid.UUID,
        endpoint: str,
        fingerprint: Mapping[str, Any],
    ) -> IdempotencyGuard | ExchangeResult:
        """Take the ``Idempotency-Key``, or return the answer it already holds.

        The claim is written inside the caller's transaction (PART 40): it commits together
        with the document, so a crash or a rollback leaves no half-claimed key and the same
        request may be retried. A key that already completed returns the **recorded payload**
        — the bytes the first call received — instead of re-running the deal.
        """
        if actor.user_id is None:  # pragma: no cover - a route always names its user
            raise PermissionDeniedError(
                "An exchange must name the user who recorded it.",
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
        return ExchangeResult(
            payload=recorded,
            status_code=replay.status_code,
            replayed=True,
            transaction_id=replay.resource_id,
            transaction_number=_optional_str(recorded.get("transaction_number")),
        )

    async def _require_branch(self, session: AsyncSession, branch_id: uuid.UUID) -> Branch:
        branch = await BranchRepository(session).get(branch_id)
        if branch is None:
            raise ResourceNotFoundError(
                "That branch does not exist.",
                details={"fields": [{"field": "branch_id", "code": "not_found"}]},
            )
        if not branch.is_active:
            raise ValidationError(
                "A closed branch cannot record new deals.",
                details={"fields": [{"field": "branch_id", "code": "inactive"}]},
            )
        return branch

    async def _branch_timezone(self, session: AsyncSession, branch_id: uuid.UUID) -> str | None:
        """The business-date timezone of a branch, read without judging whether it is open.

        Reversals happen at retired branches too (the ledger's own rule), and the document
        number of a mirror still has to roll on the branch's day.
        """
        branch = await BranchRepository(session).get(branch_id)
        return branch.timezone if branch is not None else None

    async def _resolve_device(
        self,
        session: AsyncSession,
        *,
        actor: ActorContext,
        requested: uuid.UUID | None,
        branch_id: uuid.UUID,
    ) -> uuid.UUID | None:
        """The device the deal is recorded from: the caller's own, and a live one.

        A request may state its device, but it cannot *choose* one: accepting a device the
        caller does not hold would let a client write someone else's installation into the
        audit trail (PART 17), so a mismatch is refused rather than overwritten. When the
        caller's session has no device, the stated device must still be registered to the
        document's branch — a device belongs to one branch, and cash cannot be counted at a
        counter the device was never registered for.
        """
        device_id = requested or actor.device_id
        if device_id is None:
            return None
        if requested is not None and actor.device_id is not None and requested != actor.device_id:
            raise ValidationError(
                "A deal can only be recorded from the device that is signed in.",
                details={
                    "fields": [{"field": "device_id", "code": "device_mismatch"}],
                    "authenticated_device_id": str(actor.device_id),
                },
            )
        device = await DeviceRepository(session).get_device(device_id)
        if device is None:
            raise ResourceNotFoundError(
                "That device is not registered.",
                details={"fields": [{"field": "device_id", "code": "not_found"}]},
            )
        if not device.is_active or device.revoked_at is not None:
            raise ValidationError(
                "That device is revoked.",
                details={"fields": [{"field": "device_id", "code": "revoked"}]},
            )
        if device.branch_id != branch_id:
            raise ForbiddenScopeError(
                "That device belongs to another branch.",
                details={
                    "fields": [{"field": "device_id", "code": "branch_mismatch"}],
                    "device_branch_id": str(device.branch_id),
                    "branch_id": str(branch_id),
                },
            )
        return device.id

    async def _resolve_customer(
        self, session: AsyncSession, *, customer_id: uuid.UUID | None, branch_id: uuid.UUID
    ) -> Customer | None:
        """The counterparty, when named: active, and not another branch's customer."""
        if customer_id is None:
            return None
        customer = await CustomerRepository(session).get(customer_id)
        if customer is None:
            raise ResourceNotFoundError(
                "That customer does not exist.",
                details={"fields": [{"field": "customer_id", "code": "not_found"}]},
            )
        if not customer.is_active:
            raise CustomerInactiveError(details={"customer_id": str(customer.id)})
        if customer.branch_id is not None and customer.branch_id != branch_id:
            raise ForbiddenScopeError(
                "That customer belongs to another branch.",
                details={
                    "fields": [{"field": "customer_id", "code": "branch_mismatch"}],
                    "customer_branch_id": str(customer.branch_id),
                    "branch_id": str(branch_id),
                },
            )
        return customer

    async def _replay_client_event(
        self, session: AsyncSession, *, actor: ActorContext, request: ExchangeRequest
    ) -> ExchangeResult | None:
        """Answer an offline event that was already recorded (PART 34, §19).

        A device that re-syncs an event it already sent must not be refused and must not
        produce a second deal: the same event returns the document it produced. A *different*
        deal under the same event id is a conflict — never an overwrite, because
        Last-Write-Wins is forbidden for financial records — and the refusal is recorded in
        its own transaction so it survives this request's rollback.
        """
        if request.client_event_id is None:
            return None
        existing = await ExchangeTransactionRepository(session).find_by_client_event(
            request.client_event_id
        )
        if existing is None:
            return None
        if self._same_event(existing, request):
            view = await self._load_view(session, existing.id)
            return ExchangeResult(
                payload=view.to_payload(),
                status_code=200,
                replayed=True,
                transaction_id=existing.id,
                transaction_number=existing.transaction_number,
            )
        async with self._database.transaction() as recorder:
            AuditService(recorder).record(
                action=AuditAction.EXCHANGE_EVENT_CONFLICT,
                entity_type="exchange_transaction",
                entity_id=existing.id,
                new_data={
                    "client_event_id": str(request.client_event_id),
                    "existing_transaction_id": str(existing.id),
                    "existing_transaction_number": existing.transaction_number,
                    "incoming": {
                        "transaction_type": request.transaction_type,
                        "from_amount": format_decimal(request.from_amount),
                        "exchange_rate": format_decimal(request.exchange_rate),
                    },
                    "stored": {
                        "transaction_type": existing.transaction_type,
                        "from_amount": format_decimal(existing.from_amount),
                        "exchange_rate": format_decimal(existing.exchange_rate),
                    },
                    "conflict": "CLIENT_EVENT_CONFLICT",
                },
                actor=actor,
            )
        raise ConflictError(
            "This offline event was already recorded with different content.",
            details={
                "fields": [{"field": "client_event_id", "code": "event_conflict"}],
                "client_event_id": str(request.client_event_id),
                "existing_transaction_id": str(existing.id),
                "existing_transaction_number": existing.transaction_number,
            },
        )

    @staticmethod
    def _same_event(existing: ExchangeTransaction, request: ExchangeRequest) -> bool:
        """Whether a recorded document is the same deal as a replayed offline event."""
        return (
            existing.branch_id == request.branch_id
            and existing.transaction_type == request.transaction_type
            and existing.from_currency_id == request.from_currency_id
            and existing.to_currency_id == request.to_currency_id
            and existing.from_amount == request.from_amount
            and existing.exchange_rate == request.exchange_rate
            and existing.commission == request.commission
            and existing.customer_id == request.customer_id
        )

    async def _resolve_quote(
        self,
        session: AsyncSession,
        *,
        request: ExchangeRequest,
        moment: dt.datetime,
        branch_id: uuid.UUID,
    ) -> RateSnapshot:
        """The one quote this deal is priced from, with its provenance (§6).

        The pair is resolved in the deal's own direction (``from`` → ``to``) and the side of
        the quote follows the business's side of the trade: a BUY applies the house's
        ``buy_rate``, a SELL its ``sell_rate`` (``ACCOUNTING_MODEL.md`` §4). There is no
        inverse fallback: pricing a deal from a quote published the other way round is a
        different number nobody approved, so a missing pair is ``RATE_NOT_FOUND``.

        The applied rate may differ from the published one only inside the configured band
        (``RATE_TOLERANCE_BPS``, fat-finger protection), and the snapshot — quote id, pair,
        branch, instant — is what travels into the ledger's audit trail, so "which quote was
        this priced from" stays answerable after the quote is superseded.
        """
        resolved = await ExchangeRateRepository(session).resolve(
            from_currency_id=request.from_currency_id,
            to_currency_id=request.to_currency_id,
            branch_id=branch_id,
            at=moment,
        )
        if resolved is None:
            raise RateNotFoundError(
                "No quote is in force for this pair at this branch.",
                details={
                    "from_currency_id": str(request.from_currency_id),
                    "to_currency_id": str(request.to_currency_id),
                    "branch_id": str(branch_id),
                    "as_of": moment.isoformat(),
                    "hint": "Publish a quote for this pair at this branch, or globally.",
                },
            )
        quote_row = cast("Mapping[str, Any]", resolved)
        side = "buy_rate" if request.transaction_type == "BUY" else "sell_rate"
        published = Decimal(str(quote_row[side]))
        self._assert_rate_within_tolerance(
            supplied=request.exchange_rate, published=published, published_side=side
        )
        return RateSnapshot(
            rate=request.exchange_rate,
            rate_id=uuid.UUID(str(quote_row["exchange_rate_id"])),
            from_currency_id=request.from_currency_id,
            to_currency_id=request.to_currency_id,
            branch_id=uuid.UUID(str(quote_row["branch_id"])) if quote_row["branch_id"] else None,
            effective_at=quote_row["effective_at"],
            source="RESOLVED",
        )

    def _assert_rate_within_tolerance(
        self, *, supplied: Decimal, published: Decimal, published_side: str
    ) -> None:
        """Refuse a rate the house never approved (``RATE_OUT_OF_TOLERANCE``, 409).

        The comparison is exact: ``|supplied - published| x 10000 <= published x tolerance_bps``,
        with no division and no float, so the boundary case is decided by the numbers rather
        than by a rounding artefact. A tolerance of ``0`` means the published quote must be
        used exactly.
        """
        tolerance_bps = int(self._settings.rate_tolerance_bps)
        with money_context():
            difference = money_difference(supplied, published).copy_abs()
            within = (
                supplied == published
                if tolerance_bps == 0
                else difference * 10_000 <= published * tolerance_bps
            )
            deviation_bps = divide_money(difference * 10_000, published, scale=4)
        if not within:
            raise RateOutOfToleranceError(
                "The rate is outside the tolerance of the published quote.",
                details={
                    "fields": [{"field": "exchange_rate", "code": "out_of_tolerance"}],
                    "supplied_rate": format_decimal(supplied),
                    "published_rate": format_decimal(published),
                    "published_side": published_side,
                    "deviation_bps": format_decimal(deviation_bps),
                    "tolerance_bps": tolerance_bps,
                    "hint": "Use the published rate, or ask a manager to publish a new quote.",
                },
            )

    def _assert_client_amount(
        self,
        *,
        stated: Decimal | None,
        computed: Decimal,
        decimal_places: int,
        currency_code: str,
    ) -> None:
        """Check the client's expectation against the computed settlement (§7, PART 63).

        A client may send what it believes the customer is owed — a hand-held device that
        already told the customer a number should be able to detect a disagreement instead of
        printing a receipt that contradicts the ledger. The server's value is authoritative;
        one minor unit of slack absorbs a device that rounds the last decimal differently, and
        anything larger is refused (``422 AMOUNT_MISMATCH``).
        """
        if stated is None:
            return
        allowance = Decimal(1).scaleb(-decimal_places)
        difference = money_difference(computed, stated).copy_abs()
        if difference > allowance:
            raise AmountMismatchError(
                "The stated amount is not the amount this deal settles.",
                details={
                    "fields": [{"field": "to_amount", "code": "amount_mismatch"}],
                    "computed_to_amount": format_decimal(computed),
                    "stated_to_amount": format_decimal(stated),
                    "difference": format_decimal(difference),
                    "allowed_difference": format_decimal(allowance),
                    "to_currency_code": currency_code,
                },
            )

    async def _require_chart_account(
        self, session: AsyncSession, *, code: str, purpose: str
    ) -> Account:
        """A chart account the exchange engine posts to, by its accounting-model code."""
        account = await AccountRepository(session).get_by_code(code)
        if account is None or not account.is_active or not account.is_postable:
            raise DataIntegrityError(
                f"The chart has no usable {purpose} account ({code}).",
                details={
                    "account_code": code,
                    "purpose": purpose,
                    "hint": "Re-run the chart seed, or reactivate that account.",
                },
            )
        return account

    @staticmethod
    def _settlement_movements(
        *,
        transaction_type: str,
        from_amount: Decimal,
        to_amount: Decimal,
        from_account_id: uuid.UUID,
        from_currency_id: uuid.UUID,
        to_account_id: uuid.UUID,
        to_currency_id: uuid.UUID,
        label: str,
    ) -> list[CashMovementSpec]:
        """What the two drawers physically saw (``ACCOUNTING_MODEL.md`` §6.2, §6.3).

        A BUY takes the ``from`` currency in and pays the ``to`` amount out; a SELL does the
        opposite. The commission never becomes a movement of its own: on a BUY it simply stays
        in the drawer (the payout is already net of it), on a SELL it is collected inside the
        receipt — in both cases the drawer's arithmetic agrees with the ledger by construction
        rather than by a later adjustment.

        The movements carry no ``client_event_id``: the *document* owns the offline event
        identity (``ux_exchange_transactions_client_event``), and two legs of one settlement
        are one event — the per-row unique index on that column is what makes passing it twice
        a database error instead of a duplicate-suppression mechanism.
        """
        if transaction_type == "BUY":
            return [
                CashMovementSpec(
                    account_id=from_account_id,
                    currency_id=from_currency_id,
                    movement_type="IN",
                    amount=from_amount,
                    description=f"BUY {label} acquired",
                ),
                CashMovementSpec(
                    account_id=to_account_id,
                    currency_id=to_currency_id,
                    movement_type="OUT",
                    amount=to_amount,
                    description=f"BUY {label} paid out",
                ),
            ]
        return [
            CashMovementSpec(
                account_id=to_account_id,
                currency_id=to_currency_id,
                movement_type="IN",
                amount=to_amount,
                description=f"SELL {label} received",
            ),
            CashMovementSpec(
                account_id=from_account_id,
                currency_id=from_currency_id,
                movement_type="OUT",
                amount=from_amount,
                description=f"SELL {label} delivered",
            ),
        ]

    async def _record_reversing_movements(
        self,
        session: AsyncSession,
        *,
        document: ExchangeTransaction,
        actor: ActorContext,
        journal_entry_id: uuid.UUID,
        reference_type: str,
        reference_id: uuid.UUID,
        label: str,
    ) -> None:
        """Mirror the physical side of a deal: what came in goes out, and the other way round.

        The reversing movements are built from the **posted** movements rather than from the
        deal's inputs, so a cancellation undoes exactly what the drawers recorded — including
        the account each leg used, which was resolved once, at posting time.
        """
        posted = await ExchangeTransactionRepository(session).cash_movements(
            reference_type=REFERENCE_TYPE_EXCHANGE, reference_id=document.id
        )
        if not posted:  # pragma: no cover - a document always moves cash
            raise DataIntegrityError(
                "This document has no cash movements to reverse.",
                details={"transaction_id": str(document.id), "reason": "NO_MOVEMENTS"},
            )
        movements: list[CashMovementSpec] = []
        for movement in posted:
            direction = _MOVEMENT_FLIP.get(str(movement["movement_type"]))
            if direction is None:  # pragma: no cover - exchange movements are IN/OUT
                raise DataIntegrityError(
                    "That document has a movement this engine cannot reverse.",
                    details={
                        "transaction_id": str(document.id),
                        "movement_type": movement["movement_type"],
                    },
                )
            movements.append(
                CashMovementSpec(
                    account_id=uuid.UUID(str(movement["account_id"])),
                    currency_id=uuid.UUID(str(movement["currency_id"])),
                    movement_type=direction,
                    amount=Decimal(str(movement["amount"])),
                    description=f"Reversal of {label}",
                )
            )
        await self._accounting.record_cash_movements(
            session,
            reference_type=reference_type,
            reference_id=reference_id,
            branch_id=document.branch_id,
            movements=movements,
            actor=actor,
            journal_entry_id=journal_entry_id,
            cash_session_id=document.cash_session_id,
            device_id=document.device_id,
            allow_inactive_branch=True,
        )

    async def _lock_document(
        self, session: AsyncSession, *, actor: ActorContext, transaction_id: uuid.UUID
    ) -> ExchangeTransaction:
        """Lock the document for a lifecycle move, refusing one outside the caller's scope.

        The row lock is what serialises two simultaneous cancels or reversals: the second one
        waits, then reads the state the first committed and is refused by the status rules
        instead of both acting on the same snapshot.
        """
        document = await ExchangeTransactionRepository(session).get(transaction_id, for_update=True)
        if document is None or not self._accounting.branch_in_scope(actor, document.branch_id):
            raise ResourceNotFoundError(
                "That exchange transaction does not exist.",
                details={"resource": "exchange_transaction", "id": str(transaction_id)},
            )
        return document

    @staticmethod
    def _require_status(
        document: ExchangeTransaction, *, allowed: Sequence[str], operation: str
    ) -> None:
        if document.status in allowed:
            return
        raise InvalidStatusTransitionError(
            f"A {document.status} document cannot be {operation}ed.",
            details={
                "transaction_id": str(document.id),
                "transaction_number": document.transaction_number,
                "status": document.status,
                "allowed_statuses": list(allowed),
                "operation": operation,
            },
        )

    @staticmethod
    def _assert_not_a_reversal(document: ExchangeTransaction, *, operation: str) -> None:
        """Refuse to undo a reversal document (``REVERSAL_NOT_UNDOABLE``, 409).

        A reversing document is the ledger's undo of a deal, and the original's ``REVERSED``
        status is the record that it happened. Cancelling or reversing the reversal would put
        the money back while the original stayed marked as undone — the document's status and
        the measured positions would then disagree, and an auditor reading either one would be
        misled. The honest remedy is a new deal at the current quote, which the refusal names.
        """
        if document.reversal_of_id is None:
            return
        raise ReversalNotUndoableError(
            "A reversal document cannot itself be cancelled or reversed.",
            details={
                "transaction_id": str(document.id),
                "transaction_number": document.transaction_number,
                "reversal_of_id": str(document.reversal_of_id),
                "operation": operation,
                "hint": (
                    "Record a new exchange at the current quote if the customer wants the "
                    "deal back."
                ),
            },
        )

    async def _flush(self, session: AsyncSession) -> None:
        await ExchangeTransactionRepository(session).flush()

    async def _load_view(self, session: AsyncSession, transaction_id: uuid.UUID) -> ExchangeView:
        """Read one document with its movements, or refuse if it does not exist."""
        repository = ExchangeTransactionRepository(session)
        row = await repository.view(transaction_id)
        if row is None:
            raise ResourceNotFoundError(
                "That exchange transaction does not exist.",
                details={"resource": "exchange_transaction", "id": str(transaction_id)},
            )
        movements = list(
            await repository.cash_movements(
                reference_type=REFERENCE_TYPE_EXCHANGE, reference_id=transaction_id
            )
        )
        if str(row["status"]) in (STATUS_CANCELLED, STATUS_REVERSED):
            # A cancelled document also owns the movements that undid it. A reversed one does
            # not: its reversal movements belong to the mirror document that caused them, and
            # that document is the one a reader has to follow (the payload points at it).
            movements.extend(
                await repository.cash_movements(
                    reference_type=REFERENCE_TYPE_REVERSAL, reference_id=transaction_id
                )
            )
        return ExchangeView(row=row, cash_movements=tuple(movements))


def build_exchange_service(*, database: Database, settings: Settings) -> ExchangeService:
    """The service the API and the workers construct (PART 63: routes own no logic)."""
    return ExchangeService(database=database, settings=settings)
