"""Money-transfer mapping (PART 15).

Table: ``transfers``. Lifecycle ``PENDING → APPROVED → PAID`` (or ``CANCELLED``)
is enforced by a database state machine; parties and amounts are immutable after
creation, so corrections are cancellations plus a new transfer.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class Transfer(UUIDPrimaryKeyMixin, Base):
    """A remittance between a sender and a receiver, settled at the counter."""

    __tablename__ = "transfers"

    reference_number: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    sender_name: Mapped[str] = mapped_column(String(200), nullable=False)
    sender_phone: Mapped[str | None] = mapped_column(String(50))
    receiver_name: Mapped[str] = mapped_column(String(200), nullable=False)
    receiver_phone: Mapped[str | None] = mapped_column(String(50))
    source_location: Mapped[str | None] = mapped_column(String(200))
    destination_location: Mapped[str | None] = mapped_column(String(200))
    currency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("currencies.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    commission: Mapped[Decimal] = mapped_column(
        Numeric(30, 10), nullable=False, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("'PENDING'")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    paid_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    payout_amount: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    payout_currency_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("currencies.id"))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journal_entries.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    origin: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'ONLINE'"))
    client_event_id: Mapped[uuid.UUID | None] = mapped_column()
