"""Chart of accounts and the rebuildable balance cache (PART 11, PART 12, PART 46).

Tables: ``accounts``, ``account_balances``.

``account_balances`` is a *cache*. The source of truth is ``journal_lines``;
``rebuild_account_balances()`` in the database reproduces the cache exactly, and
invariant I-2 asserts that equality.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TrimmedChar, UUIDPrimaryKeyMixin


class Account(UUIDPrimaryKeyMixin, Base):
    """A ledger account. ``normal_balance`` is derived from ``account_type`` at seed time."""

    __tablename__ = "accounts"

    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    account_type: Mapped[str] = mapped_column(String(50), nullable=False)
    currency_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("currencies.id"))
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    is_postable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    normal_balance: Mapped[str | None] = mapped_column(TrimmedChar(6))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class AccountBalance(UUIDPrimaryKeyMixin, Base):
    """Per (account, currency) totals — a cache rebuilt from the ledger."""

    __tablename__ = "account_balances"
    __table_args__ = (
        UniqueConstraint("account_id", "currency_id", name="ux_account_balances_account_currency"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    currency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("currencies.id", ondelete="RESTRICT"), nullable=False
    )
    debit_total: Mapped[Decimal] = mapped_column(
        Numeric(30, 10), nullable=False, server_default=text("0")
    )
    credit_total: Mapped[Decimal] = mapped_column(
        Numeric(30, 10), nullable=False, server_default=text("0")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
