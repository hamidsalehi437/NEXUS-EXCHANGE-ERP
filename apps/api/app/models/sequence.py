"""Document numbering counters (PART 14, PART 15).

Table: ``sequences``. Numbers are allocated by ``next_document_number()`` in the
database, which increments the counter for ``scope:YYYYMMDD`` atomically, so
per-day, per-document-family numbering can never collide.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DocumentSequence(Base):
    """Counter row keyed by ``<scope>:<YYYYMMDD>``."""

    __tablename__ = "sequences"

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    current_value: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
