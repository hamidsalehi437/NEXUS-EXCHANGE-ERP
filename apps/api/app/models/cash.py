"""Cash-control mapping (PART 16, PART 30).

Tables: ``cash_sessions``, ``cash_session_lines``, ``cash_movements``.

``cash_movements.amount`` is the **physical quantity** in its own currency;
``signed_amount`` is generated from ``movement_type`` and ``adjustment_sign`` and
is the canonical sign used by the non-negative-position constraint and by the
``v_cash_position`` view. Movements are append-only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    Computed,
    DateTime,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin

SIGNED_AMOUNT_EXPRESSION = (
    "CASE "
    "WHEN movement_type IN ('OPENING', 'IN') THEN amount "
    "WHEN movement_type IN ('OUT', 'EXPENSE') THEN -amount "
    "WHEN movement_type = 'ADJUSTMENT' THEN amount * COALESCE(adjustment_sign, 0) "
    "ELSE 0::NUMERIC END"
)


class CashSession(UUIDPrimaryKeyMixin, Base):
    """A drawer shift: opened by an operator, closed against a physical count."""

    __tablename__ = "cash_sessions"

    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), nullable=False)
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    opened_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    opened_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    closed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'OPEN'"))
    notes: Mapped[str | None] = mapped_column(Text)


class CashSessionLine(UUIDPrimaryKeyMixin, Base):
    """Per-currency reconciliation line of a shift close."""

    __tablename__ = "cash_session_lines"
    __table_args__ = (
        UniqueConstraint("cash_session_id", "currency_id", name="ux_cash_session_lines_currency"),
    )

    cash_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cash_sessions.id", ondelete="CASCADE"), nullable=False
    )
    currency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("currencies.id"), nullable=False)
    opening_declared: Mapped[Decimal] = mapped_column(
        Numeric(30, 10), nullable=False, server_default=text("0")
    )
    expected_amount: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    counted_amount: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    difference: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))


class CashMovement(UUIDPrimaryKeyMixin, Base):
    """An immutable physical cash movement in one currency."""

    __tablename__ = "cash_movements"

    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    currency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("currencies.id"), nullable=False)
    movement_type: Mapped[str] = mapped_column(String(30), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(50))
    reference_id: Mapped[uuid.UUID | None] = mapped_column()
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    adjustment_sign: Mapped[int | None] = mapped_column(SmallInteger)
    cash_session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cash_sessions.id"))
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journal_entries.id"))
    client_event_id: Mapped[uuid.UUID | None] = mapped_column()
    signed_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 10), Computed(SIGNED_AMOUNT_EXPRESSION, persisted=True)
    )
