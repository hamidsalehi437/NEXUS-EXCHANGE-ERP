"""Currency mapping (PART 9).

Table: ``currencies``. Exactly one row may carry ``is_base = TRUE`` — enforced by
the partial unique index ``ux_currencies_single_base`` in the database.
``decimal_places`` drives presentation rounding only; storage is always
``NUMERIC(30,10)``.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, SmallInteger, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class Currency(UUIDPrimaryKeyMixin, Base):
    """A currency the business quotes, holds or settles in."""

    __tablename__ = "currencies"

    code: Mapped[str] = mapped_column(String(10), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(20))
    decimal_places: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("2")
    )
    is_base: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    is_tradable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    display_order: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
