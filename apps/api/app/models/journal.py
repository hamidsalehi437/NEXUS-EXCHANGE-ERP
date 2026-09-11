"""Double-entry ledger mapping (PART 12, PART 49).

Tables: ``journal_entries``, ``journal_lines``.

Money semantics (see ``docs/architecture/ACCOUNTING_MODEL.md`` §3): ``debit`` and
``credit`` are amounts in the **functional (base) currency**, ``currency_id`` is
the currency the account holds, ``exchange_rate`` is functional units per one
unit of that currency, and ``foreign_amount`` is generated from them.

Both tables are append-only: the database forbids UPDATE/DELETE, and the
application role has no such privileges. Corrections are reversals.
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
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin

FOREIGN_AMOUNT_EXPRESSION = (
    "CASE WHEN exchange_rate IS NULL OR exchange_rate = 0 THEN NULL "
    "ELSE (debit + credit) / exchange_rate END"
)


class JournalEntry(UUIDPrimaryKeyMixin, Base):
    """A balanced double-entry document, posted from exactly one business event."""

    __tablename__ = "journal_entries"

    reference_type: Mapped[str] = mapped_column(String(50), nullable=False)
    reference_id: Mapped[uuid.UUID | None] = mapped_column()
    description: Mapped[str | None] = mapped_column(Text)
    transaction_date: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id"))
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    reversal_of_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journal_entries.id"))


class JournalLine(UUIDPrimaryKeyMixin, Base):
    """One side of a balanced entry (exactly one of debit/credit is positive)."""

    __tablename__ = "journal_lines"

    journal_entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    debit: Mapped[Decimal] = mapped_column(
        Numeric(30, 10), nullable=False, server_default=text("0")
    )
    credit: Mapped[Decimal] = mapped_column(
        Numeric(30, 10), nullable=False, server_default=text("0")
    )
    currency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("currencies.id"), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(
        Numeric(30, 10), nullable=False, server_default=text("1")
    )
    description: Mapped[str | None] = mapped_column(Text)
    foreign_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 10), Computed(FOREIGN_AMOUNT_EXPRESSION, persisted=True)
    )
