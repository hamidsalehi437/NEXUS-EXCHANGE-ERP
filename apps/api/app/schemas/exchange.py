"""Exchange-document schemas (API_CONTRACT §9.3, PART 62, PART 63).

Three rules shape this module:

* **Money is a decimal string.** ``from_amount``, ``to_amount``, ``exchange_rate`` and
  ``commission`` arrive as strings and leave as strings with the stored scale; a JSON
  number is refused at the door by ``to_decimal`` (PART 62), because a float has already
  lost precision by the time any validator could see it.
* **The server computes, the client cross-checks.** ``to_amount`` is optional in a create
  request and is treated as the *client's expectation*, never as the value to store
  (PART 63): the service computes the settlement from the rate and the commission, and a
  disagreement beyond one minor unit is refused with ``422 AMOUNT_MISMATCH``.
* **One response shape.** Create, read, cancel, reverse and the receipt all describe the
  same document, so a client can parse one model and receive the applied rate, the journal
  reference, the reversal linkage and the physical cash movements no matter which door it
  knocked on. Replays of an idempotent request validate against the same model, which is
  what makes "the recorded answer, byte for byte" true rather than aspirational.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.core.money import MoneyError, assert_within_money_bounds, format_decimal, to_decimal

# What a create request may ask for. The direction vocabulary is the domain's (the frozen
# ``ck_exchange_transactions_type``), stated here so a typo is a 422 from the edge instead of
# a domain refusal deeper in.
ExchangeType = Literal["BUY", "SELL"]

# The receipt is rendered by the client from this payload; the version travels with it so a
# stored or printed receipt can be re-rendered the way it was issued.
RECEIPT_VERSION = "phase5-1"


def _money_from_wire(value: Any, field: str) -> Decimal:
    """Parse a money field that may arrive as a string, int or Decimal — never a float."""
    try:
        parsed = to_decimal(value, field=field)
        assert_within_money_bounds(parsed, field=field)
    except MoneyError as exc:
        raise ValueError(str(exc)) from exc
    return parsed


class ExchangeCreateRequest(BaseModel):
    """``POST /api/v1/exchange`` body — one deal, identified by its ``Idempotency-Key``."""

    model_config = ConfigDict(extra="forbid")

    transaction_type: ExchangeType = Field(
        description="BUY: the business acquires from_currency. SELL: the business delivers it."
    )
    branch_id: uuid.UUID = Field(description="The branch whose drawers the deal moves")
    from_currency_id: uuid.UUID = Field(
        description="The currency the business receives (BUY) or delivers (SELL)"
    )
    from_amount: Decimal = Field(description="Quantity of from_currency, as a decimal string")
    to_currency_id: uuid.UUID = Field(
        description="The currency the business pays (BUY) or receives (SELL)"
    )
    exchange_rate: Decimal = Field(
        description="Applied quote: units of to_currency per one unit of from_currency"
    )
    commission: Decimal = Field(
        default=Decimal(0), description="Commission charged in to_currency (never negative)"
    )
    to_amount: Decimal | None = Field(
        default=None,
        description=(
            "Optional client expectation for the settled to-side amount. The server computes "
            "the authoritative value; a disagreement beyond one minor unit is refused."
        ),
    )
    customer_id: uuid.UUID | None = Field(default=None, description="Counterparty, when identified")
    device_id: uuid.UUID | None = Field(
        default=None,
        description="The device that recorded the deal; must be the caller's own device",
    )
    client_event_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Offline origin: the client-side event id that makes a replayed sync a no-op "
            "and a conflicting one visible"
        ),
    )
    transaction_date: dt.datetime | None = Field(
        default=None,
        description="Accounting date (UTC). Defaults to now; the future is refused.",
    )
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("from_amount", "exchange_rate", "commission", "to_amount", mode="before")
    @classmethod
    def _parse_money(cls, value: Any, info: Any) -> Any:
        if value is None:
            return None
        return _money_from_wire(value, info.field_name)

    @field_validator("commission")
    @classmethod
    def _non_negative(cls, value: Decimal, info: Any) -> Decimal:
        if value < 0:
            raise ValueError(f"{info.field_name} cannot be negative")
        return value

    @field_validator("transaction_date")
    @classmethod
    def _aware(cls, value: dt.datetime | None) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("transaction_date must carry a timezone (UTC is stored)")
        return value.astimezone(dt.UTC)

    @field_validator("description", mode="before")
    @classmethod
    def _clean(cls, value: object) -> object:
        """Trim, and treat an empty string as absent (a blank note is not a note)."""
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_serializer("from_amount", "exchange_rate", "commission", "to_amount")
    def _serialize_money(self, value: Decimal | None) -> str | None:
        return None if value is None else format_decimal(value)


class ExchangeReasonRequest(BaseModel):
    """``POST /exchange/{id}/cancel`` and ``/reverse`` body — the auditor reads the reason."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500, description="Why the document is undone")

    @field_validator("reason", mode="before")
    @classmethod
    def _clean(cls, value: object) -> object:
        if isinstance(value, str):
            return " ".join(value.split())
        return value


class ExchangeCashMovementResponse(BaseModel):
    """One physical cash movement the document produced (what a drawer actually saw)."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    movement_type: str = Field(description="IN, OUT, OPENING, ADJUSTMENT, EXPENSE or CLOSING")
    amount: str = Field(description="Quantity of the currency, as a decimal string")
    currency_id: uuid.UUID
    currency_code: str
    account_id: uuid.UUID
    account_code: str
    signed_amount: str = Field(description="Database-generated signed quantity of the position")
    journal_entry_id: uuid.UUID | None = None
    reference_type: str | None = Field(
        default=None,
        description=(
            "What the movement belongs to: EXCHANGE_TRANSACTION for the deal (or a reversing "
            "document), REVERSAL for a cancellation's own reversal movements"
        ),
    )
    created_at: dt.datetime


class ExchangeReceiptReference(BaseModel):
    """Where the printable receipt of this document lives (PDF rendering is Phase 11)."""

    model_config = ConfigDict(extra="forbid")

    url: str


class ExchangeDocument(BaseModel):
    """One exchange document as the API returns it (create, read, cancel, reverse)."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    transaction_number: str
    status: str = Field(description="PENDING, COMPLETED, CANCELLED or REVERSED")
    transaction_type: str = Field(description="BUY or SELL")
    origin: str = Field(description="ONLINE or OFFLINE — where the deal was recorded")

    branch_id: uuid.UUID
    branch_code: str
    branch_name: str
    cashier_id: uuid.UUID
    cashier_username: str
    customer_id: uuid.UUID | None = None
    customer_code: str | None = None
    customer_name: str | None = None
    device_id: uuid.UUID | None = None
    cash_session_id: uuid.UUID | None = Field(
        default=None, description="The drawer shift this deal belongs to, when one was open"
    )
    client_event_id: uuid.UUID | None = None

    from_currency_id: uuid.UUID
    from_currency_code: str
    from_amount: str
    to_currency_id: uuid.UUID
    to_currency_code: str
    to_amount: str = Field(description="The settled to-side amount the server computed")
    exchange_rate: str = Field(description="The applied quote, as stored on the document")
    commission: str
    gross_amount: str = Field(
        description=(
            "The to-side amount before the commission, reconstructed from the persisted "
            "columns (BUY: to_amount + commission; SELL: to_amount)"
        )
    )

    journal_entry_id: uuid.UUID | None = None
    journal_total_debit: str | None = None
    journal_total_credit: str | None = None

    reversal_of_id: uuid.UUID | None = None
    reversal_transaction_id: uuid.UUID | None = None
    reversal_transaction_number: str | None = None
    reversal_journal_entry_id: uuid.UUID | None = None
    reversal_reason: str | None = None
    reversed_by: uuid.UUID | None = None
    reversed_at: dt.datetime | None = None

    version: int
    created_at: dt.datetime
    updated_at: dt.datetime
    cash_movements: list[ExchangeCashMovementResponse] = Field(default_factory=list)
    receipt: ExchangeReceiptReference


class ExchangeListResponse(BaseModel):
    """One page of the branch's exchange book, newest first."""

    model_config = ConfigDict(extra="forbid")

    items: list[ExchangeDocument]
    total: int
    limit: int
    offset: int


class ExchangeReceiptLine(BaseModel):
    """One settlement movement as the customer sees it."""

    model_config = ConfigDict(extra="forbid")

    movement_type: str
    currency_code: str
    amount: str


class ExchangeReceipt(BaseModel):
    """Deterministic receipt payload for A4/thermal rendering (API_CONTRACT §9.3).

    Deterministic means *reproducible*: every field comes from committed rows, nothing is
    generated at render time, and asking twice for the same document returns the same
    payload — including after later quotes have been published, because the receipt shows
    the document's own applied rate rather than "today's" rate.
    """

    model_config = ConfigDict(extra="forbid")

    receipt_version: str
    transaction_number: str
    status: str
    transaction_type: str
    issued_at: dt.datetime
    business_date: str = Field(
        description="The branch's business date (its own timezone), YYYY-MM-DD"
    )

    branch_code: str
    branch_name: str
    branch_address: str | None = None
    branch_phone: str | None = None

    cashier_username: str
    customer_code: str | None = None
    customer_name: str | None = None

    from_currency_code: str
    from_amount: str
    to_currency_code: str
    to_amount: str
    exchange_rate: str
    gross_amount: str
    commission: str

    settlement: list[ExchangeReceiptLine] = Field(
        default_factory=list,
        description=(
            "The money this document itself moved, in the order IN then OUT. A reversal "
            "document reports the ledger's reversal pair as its own settlement."
        ),
    )
    reversal_settlement: list[ExchangeReceiptLine] = Field(
        default_factory=list,
        description=(
            "The money the *undo* of this document moved back, for a cancelled or reversed "
            "deal; empty for a document that was never undone."
        ),
    )
    journal_entry_id: uuid.UUID | None = None
    reversal_of_id: uuid.UUID | None = None
    reversal_transaction_number: str | None = None
    origin: str
    document_id: uuid.UUID


__all__ = [
    "RECEIPT_VERSION",
    "ExchangeCashMovementResponse",
    "ExchangeCreateRequest",
    "ExchangeDocument",
    "ExchangeListResponse",
    "ExchangeReasonRequest",
    "ExchangeReceipt",
    "ExchangeReceiptLine",
    "ExchangeReceiptReference",
    "ExchangeType",
]
