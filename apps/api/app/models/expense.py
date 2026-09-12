"""Expense mapping (PART 17).

Table: ``expenses``. Expenses are posted once to the ledger and corrected by
cancellation (which posts a reversal), never by editing or deleting the row.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class Expense(UUIDPrimaryKeyMixin, Base):
    """An operating expense of a branch."""

    __tablename__ = "expenses"

    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id"))
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    currency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("currencies.id"), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    expense_date: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'POSTED'"))
    payee: Mapped[str | None] = mapped_column(String(200))
    attachment_path: Mapped[str | None] = mapped_column(Text)
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journal_entries.id"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
