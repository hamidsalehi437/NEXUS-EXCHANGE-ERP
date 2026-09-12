"""Exchange transaction mapping (PART 14, PART 22).

Table: ``exchange_transactions``.

Money and identity columns are frozen after posting (database trigger): an
incorrect transaction is corrected by a **reversal** row that mirrors it, never by
an edit or a delete. ``client_event_id`` makes offline ingestion idempotent.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class ExchangeTransaction(UUIDPrimaryKeyMixin, Base):
    """A completed buy or sell of one currency for another."""

    __tablename__ = "exchange_transactions"

    transaction_number: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), nullable=False)
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    cashier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))
    transaction_type: Mapped[str] = mapped_column(String(20), nullable=False)
    from_currency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("currencies.id"), nullable=False)
    from_amount: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    to_currency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("currencies.id"), nullable=False)
    to_amount: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    commission: Mapped[Decimal] = mapped_column(
        Numeric(30, 10), nullable=False, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("'COMPLETED'")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    reversal_of_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("exchange_transactions.id"))
    reversal_reason: Mapped[str | None] = mapped_column(Text)
    reversed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reversed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journal_entries.id"))
    reversal_journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journal_entries.id")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    origin: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'ONLINE'"))
    client_event_id: Mapped[uuid.UUID | None] = mapped_column()
    cash_session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cash_sessions.id"))
