"""Cash-control schemas (API_CONTRACT §9.4, PART 62, PART 63).

The two rules that shape the exchange schemas hold here unchanged:

* **Money is a decimal string.** A count, an amount, an opening balance and a rate arrive as
  strings and leave as strings at the stored scale; a JSON number is refused at the door by
  ``to_decimal`` (PART 62), because a float has already lost precision before any validator
  could look at it.
* **The server owns the arithmetic and the clock.** Everything the operator writes down is
  either an amount or a direction; the expected balance, the difference, the rate a foreign
  movement is valued at and the shift's business date are all derived server-side (§9.4), so
  a client cannot post a total it computed itself (PART 63).

Requests forbid unknown fields (``extra="forbid"``) so a misspelled key is a 422 at the edge
rather than a silently ignored instruction — the failure mode that matters most for cash:
``"amout": "500"`` must never post nothing and answer 201.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.core.money import MoneyError, assert_within_money_bounds, format_decimal, to_decimal

# The direction vocabulary of a correction (the frozen ``ck_cash_movements_adjustment_sign``).
AdjustmentSign = Literal[-1, 1]


def _money_from_wire(value: Any, field: str) -> Decimal:
    """Parse a money field that may arrive as a string, int or Decimal — never a float."""
    try:
        parsed = to_decimal(value, field=field)
        assert_within_money_bounds(parsed, field=field)
    except MoneyError as exc:
        raise ValueError(str(exc)) from exc
    return parsed


class _MoneyModel(BaseModel):
    """Shared plumbing: nothing here accepts an unknown field or a float."""

    model_config = ConfigDict(extra="forbid")

    # ``check_fields=False``: the base declares the rule once for every request that carries
    # an accounting date, and a request without one (a close) simply has nothing to check.
    @field_validator("transaction_date", check_fields=False)
    @classmethod
    def _aware(cls, value: dt.datetime | None) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("transaction_date must carry a timezone (UTC is stored)")
        return value.astimezone(dt.UTC)


class CashOpeningLine(BaseModel):
    """One currency's declared opening count (``POST /cash/open``)."""

    model_config = ConfigDict(extra="forbid")

    currency_id: uuid.UUID
    amount: Decimal = Field(description="The cash the drawer holds in this currency, as a string")
    exchange_rate: Decimal | None = Field(
        default=None,
        description=(
            "Opening rate for a non-functional currency (§6.1). Omitted, the ledger values "
            "the opening at the house's own quote for that currency."
        ),
    )

    @field_validator("amount", "exchange_rate", mode="before")
    @classmethod
    def _parse_money(cls, value: Any, info: Any) -> Any:
        if value is None:
            return None
        return _money_from_wire(value, info.field_name)

    @field_serializer("amount", "exchange_rate")
    def _serialize_money(self, value: Decimal | None) -> str | None:
        return None if value is None else format_decimal(value)


class CashOpenRequest(_MoneyModel):
    """``POST /api/v1/cash/open`` body."""

    branch_id: uuid.UUID = Field(description="The branch whose drawer is being opened")
    device_id: uuid.UUID | None = Field(
        default=None,
        description="The terminal the shift is bound to; defaults to the caller's device",
    )
    openings: list[CashOpeningLine] = Field(
        default_factory=list,
        description="One entry per currency the operator counts into the drawer at open",
    )
    notes: str | None = Field(default=None, max_length=2000)


class _CashMovementBase(_MoneyModel):
    """The fields every standalone movement shares (``/cash/in``, ``/cash/out``)."""

    branch_id: uuid.UUID
    currency_id: uuid.UUID
    amount: Decimal = Field(description="The quantity that moved, as a decimal string")
    session_id: uuid.UUID | None = Field(
        default=None,
        description="The shift to post into; defaults to the caller's open shift at the branch",
    )
    device_id: uuid.UUID | None = Field(
        default=None, description="The terminal that recorded the movement"
    )
    description: str | None = Field(default=None, max_length=2000)
    client_event_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Offline origin: the client-side event id, which makes a replayed receipt a no-op "
            "instead of a second movement"
        ),
    )
    transaction_date: dt.datetime | None = Field(
        default=None, description="Accounting date (UTC). Defaults to now; the future is refused."
    )

    @field_validator("amount", mode="before")
    @classmethod
    def _parse_amount(cls, value: Any) -> Any:
        return _money_from_wire(value, "amount")

    @field_serializer("amount")
    def _serialize_amount(self, value: Decimal) -> str:
        return format_decimal(value)

    @field_validator("description", mode="before")
    @classmethod
    def _clean(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class CashInRequest(_CashMovementBase):
    """``POST /api/v1/cash/in`` body — cash received into the drawer (§6.4)."""

    source_account_id: uuid.UUID = Field(
        description="The account the value came from; the ledger posts the other leg to it"
    )


class CashOutRequest(_CashMovementBase):
    """``POST /api/v1/cash/out`` body — cash paid out of the drawer (§6.4)."""

    target_account_id: uuid.UUID = Field(
        description="The account the value went to; the ledger posts the other leg to it"
    )


class CashAdjustmentRequest(_CashMovementBase):
    """``POST /api/v1/cash/adjustment`` body — a short/over correction against 5090 (§6.4)."""

    adjustment_sign: AdjustmentSign = Field(
        description="+1 when the drawer holds more than it should, -1 when it holds less"
    )
    reason: str = Field(min_length=3, max_length=500, description="Why the correction is posted")

    @field_validator("reason", mode="before")
    @classmethod
    def _clean_reason(cls, value: object) -> object:
        if isinstance(value, str):
            return " ".join(value.split())
        return value


class CashCountLine(BaseModel):
    """One currency's physical count (``POST /cash/close``)."""

    model_config = ConfigDict(extra="forbid")

    currency_id: uuid.UUID
    amount: Decimal = Field(description="What the operator actually counted, as a string")

    @field_validator("amount", mode="before")
    @classmethod
    def _parse_amount(cls, value: Any) -> Any:
        return _money_from_wire(value, "amount")

    @field_serializer("amount")
    def _serialize_amount(self, value: Decimal) -> str:
        return format_decimal(value)


class CashCloseRequest(_MoneyModel):
    """``POST /api/v1/cash/close`` body."""

    session_id: uuid.UUID = Field(description="The shift being closed")
    counted: Annotated[list[CashCountLine], Field(min_length=1)] = Field(
        description="One entry per currency the shift moved; the difference is derived"
    )
    notes: str | None = Field(default=None, max_length=2000)


class CashReverseRequest(BaseModel):
    """``POST /api/v1/cash/movements/{id}/reverse`` body."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500, description="The auditor reads it")

    @field_validator("reason", mode="before")
    @classmethod
    def _clean(cls, value: object) -> object:
        if isinstance(value, str):
            return " ".join(value.split())
        return value


class CashMovementResponse(BaseModel):
    """One physical cash movement as the API returns it."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    branch_id: uuid.UUID
    branch_code: str
    account_id: uuid.UUID
    account_code: str
    currency_id: uuid.UUID
    currency_code: str
    movement_type: str = Field(description="OPENING, IN, OUT, ADJUSTMENT, EXPENSE or CLOSING")
    amount: str = Field(description="Quantity of the currency, as a decimal string")
    signed_amount: str = Field(description="Database-generated signed quantity of the position")
    adjustment_sign: int | None = None
    reference_type: str | None = Field(
        default=None,
        description=(
            "What the movement belongs to: CASH_MOVEMENT for a door movement, OPENING_BALANCE "
            "for a shift opening, REVERSAL for a compensating movement"
        ),
    )
    reference_id: uuid.UUID | None = None
    description: str | None = None
    session_id: uuid.UUID | None = None
    session_status: str | None = None
    device_id: uuid.UUID | None = None
    journal_entry_id: uuid.UUID | None = None
    client_event_id: uuid.UUID | None = None
    reversed_by_movement_id: uuid.UUID | None = Field(
        default=None, description="The compensating movement, when this one has been reversed"
    )
    created_by: uuid.UUID | None = None
    created_by_username: str | None = None
    created_at: dt.datetime


class CashSessionLineResponse(BaseModel):
    """One reconciliation line of a shift (a currency's expected and counted amounts)."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    currency_id: uuid.UUID
    currency_code: str
    currency_name: str
    currency_decimal_places: int
    opening_declared: str
    expected_amount: str | None = Field(
        default=None,
        description="Null while the shift is open: the expectation is derived at close",
    )
    counted_amount: str | None = None
    difference: str | None = Field(
        default=None, description="counted - expected, computed by the database at close"
    )
    adjustment_journal_entry_id: uuid.UUID | None = Field(
        default=None, description="The entry that posted the variance, when there was one"
    )


class CashVarianceResponse(BaseModel):
    """The variance the close posted for one currency (§9.4)."""

    model_config = ConfigDict(extra="forbid")

    currency_id: uuid.UUID
    currency_code: str
    opening_declared: str
    expected_amount: str
    counted_amount: str
    difference: str
    movement_id: uuid.UUID
    journal_entry_id: uuid.UUID | None = None


class CashSessionResponse(BaseModel):
    """One drawer shift with its lines, its movements and its variances."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    session_id: uuid.UUID
    branch_id: uuid.UUID
    branch_code: str
    branch_name: str
    branch_timezone: str
    business_date: str = Field(
        description="The branch-local calendar day the shift opened on (server-derived)"
    )
    device_id: uuid.UUID | None = None
    device_uuid: uuid.UUID | None = None
    device_name: str | None = None
    status: str = Field(description="OPEN or CLOSED")
    opened_by: uuid.UUID | None = None
    opened_by_username: str | None = None
    opened_at: dt.datetime
    closed_by: uuid.UUID | None = None
    closed_by_username: str | None = None
    closed_at: dt.datetime | None = None
    notes: str | None = None
    lines: list[CashSessionLineResponse] = Field(default_factory=list)
    movements: list[CashMovementResponse] = Field(default_factory=list)
    movement_count: int = 0
    has_variance: bool = False
    variances: list[CashVarianceResponse] = Field(
        default_factory=list, description="Set by a close that had to post a difference"
    )


class CashSessionListResponse(BaseModel):
    """One page of shift history, newest first."""

    model_config = ConfigDict(extra="forbid")

    items: list[CashSessionResponse]
    total: int
    limit: int
    offset: int


class CashMovementListResponse(BaseModel):
    """One page of the cash movement book, newest first."""

    model_config = ConfigDict(extra="forbid")

    items: list[CashMovementResponse]
    total: int
    limit: int
    offset: int


class CashBalanceRowResponse(BaseModel):
    """One branch/currency position: the movements beside the ledger."""

    model_config = ConfigDict(extra="forbid")

    branch_id: uuid.UUID
    branch_code: str
    currency_id: uuid.UUID
    currency_code: str
    physical_balance: str = Field(description="Quantity of the currency the drawers hold")
    ledger_functional_balance: str = Field(description="What the journal carries, functionally")
    ledger_quantity: str = Field(description="What the journal carries, in the currency")
    reconciled: bool = Field(
        description="Whether the physical position and the ledger agree; a mismatch is reported"
    )
    source: str


class CashBalanceResponse(BaseModel):
    """``GET /api/v1/cash/balance`` — per branch and currency, never aggregated across them."""

    model_config = ConfigDict(extra="forbid")

    items: list[CashBalanceRowResponse]
    source: str = Field(description="cash_movements, the immutable physical evidence")
    generated_at: dt.datetime


__all__ = [
    "AdjustmentSign",
    "CashAdjustmentRequest",
    "CashBalanceResponse",
    "CashBalanceRowResponse",
    "CashCloseRequest",
    "CashCountLine",
    "CashInRequest",
    "CashMovementListResponse",
    "CashMovementResponse",
    "CashOpenRequest",
    "CashOpeningLine",
    "CashOutRequest",
    "CashReverseRequest",
    "CashSessionLineResponse",
    "CashSessionListResponse",
    "CashSessionResponse",
    "CashVarianceResponse",
]
