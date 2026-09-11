"""Exchange rate mapping (PART 13).

Table: ``exchange_rates``. Quotes are append-only: a new quote is a new row, and
the pair/branch/instant combination is unique (``ux_exchange_rates_no_duplicate_instant``).
Resolution order is branch quote first, then the newest ``effective_at <= now``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class ExchangeRate(UUIDPrimaryKeyMixin, Base):
    """A quoted buy/sell rate for a currency pair, optionally branch-specific."""

    __tablename__ = "exchange_rates"

    from_currency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("currencies.id"), nullable=False)
    to_currency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("currencies.id"), nullable=False)
    buy_rate: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    sell_rate: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    effective_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id"))
    source: Mapped[str] = mapped_column(String(50), nullable=False, server_default=text("'MANUAL'"))
