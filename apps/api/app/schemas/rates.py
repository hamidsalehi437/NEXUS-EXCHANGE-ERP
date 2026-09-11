"""Exchange-rate schemas (API_CONTRACT §9.2, PART 13, PART 62).

Rates are **money-shaped numbers**, so they follow the same rule as amounts: a JSON
float is refused outright (``to_decimal`` rejects ``float``/``bool``), the value travels
as a decimal string on the wire, and the stored precision is ``NUMERIC(30,10)``. A rate
that arrived as a binary float would already have lost precision before any validation
could see it, which is exactly what PART 62 forbids.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.core.money import MoneyError, assert_within_money_bounds, format_decimal, to_decimal

# ``MANUAL`` is what an operator types; the other three are recorded when a quote is
# imported (a central-bank feed, a partner desk), which is why the seed of the catalogue
# keeps them: the value tells an auditor where a rate came from.
RATE_SOURCES = ("MANUAL", "IMPORT", "CENTRAL_BANK", "PARTNER")


def _decimal_from_wire(value: Any, field: str) -> Decimal:
    """Parse a rate that may only arrive as a string, int or Decimal — never a float."""
    try:
        parsed = to_decimal(value, field=field)
        assert_within_money_bounds(parsed, field=field)
    except MoneyError as exc:
        raise ValueError(str(exc)) from exc
    return parsed


class RateQuoteRequest(BaseModel):
    """``POST /api/v1/rates`` body — one appended quote."""

    model_config = ConfigDict(extra="forbid")

    from_currency_id: uuid.UUID
    to_currency_id: uuid.UUID
    buy_rate: Decimal = Field(
        description="Decimal string; the house buys the base pair at this rate"
    )
    sell_rate: Decimal = Field(
        description="Decimal string; the house sells the base pair at this rate"
    )
    effective_at: dt.datetime | None = Field(
        default=None,
        description="When the quote takes effect; defaults to now. Must be UTC if sent.",
    )
    branch_id: uuid.UUID | None = Field(
        default=None, description="Branch-specific quote; omit for a global quote"
    )
    source: str = Field(default="MANUAL", description=f"One of {', '.join(RATE_SOURCES)}")

    @field_validator("buy_rate", "sell_rate", mode="before")
    @classmethod
    def _parse_rates(cls, value: Any, info: Any) -> Decimal:
        return _decimal_from_wire(value, info.field_name)

    @field_validator("buy_rate", "sell_rate")
    @classmethod
    def _positive(cls, value: Decimal, info: Any) -> Decimal:
        if value <= 0:
            raise ValueError(f"{info.field_name} must be greater than zero")
        return value

    @field_validator("effective_at")
    @classmethod
    def _aware(cls, value: dt.datetime | None) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("effective_at must carry a timezone (UTC is stored)")
        return value.astimezone(dt.UTC)

    @field_validator("source", mode="before")
    @classmethod
    def _source(cls, value: Any) -> Any:
        if isinstance(value, str):
            upper = value.strip().upper()
            if upper not in RATE_SOURCES:
                raise ValueError(f"source must be one of {', '.join(RATE_SOURCES)}")
            return upper
        return value

    @field_serializer("buy_rate", "sell_rate")
    def _serialize(self, value: Decimal) -> str:
        return format_decimal(value)


class RateQuote(BaseModel):
    """One quote row as returned by the API."""

    id: uuid.UUID
    from_currency_id: uuid.UUID
    to_currency_id: uuid.UUID
    from_currency_code: str | None = None
    to_currency_code: str | None = None
    buy_rate: Decimal
    sell_rate: Decimal
    effective_at: dt.datetime
    branch_id: uuid.UUID | None = None
    branch_code: str | None = None
    source: str
    created_at: dt.datetime
    created_by: uuid.UUID | None = None

    @field_serializer("buy_rate", "sell_rate")
    def _serialize(self, value: Decimal) -> str:
        return format_decimal(value)


class RateListResponse(BaseModel):
    """Envelope for the quote tables."""

    items: list[RateQuote]
    total: int
    limit: int
    offset: int


class RateResolution(BaseModel):
    """The quote a transaction would receive right now (rate-resolution service)."""

    from_currency_id: uuid.UUID
    to_currency_id: uuid.UUID
    exchange_rate_id: uuid.UUID
    buy_rate: Decimal
    sell_rate: Decimal
    effective_at: dt.datetime
    branch_id: uuid.UUID | None = None
    is_branch_quote: bool = Field(
        description="True when a branch-specific quote overrode the global one"
    )

    @field_serializer("buy_rate", "sell_rate")
    def _serialize(self, value: Decimal) -> str:
        return format_decimal(value)
