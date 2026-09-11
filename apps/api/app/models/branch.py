"""Branch mapping (PART 7, PART 9).

Table: ``branches``. ``timezone`` is the *display/business-date* timezone; every
timestamp in the database is stored in UTC.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class Branch(UUIDPrimaryKeyMixin, Base):
    """A physical office. Devices, cash and inventory accounts belong to a branch."""

    __tablename__ = "branches"

    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(50))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'Asia/Kabul'")
    )
