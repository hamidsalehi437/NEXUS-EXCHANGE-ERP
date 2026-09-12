"""Server change stream for offline pull (PART 33, PART 34).

Table: ``change_log``. Rows are produced by database triggers on the syncable
tables; devices consume them by increasing ``seq`` and persist their position in
``sync_cursors``. Retention is governed by ``CHANGE_LOG_RETENTION_DAYS``.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ChangeLog(Base):
    """One change-stream entry (CREATE/UPDATE/CANCEL/REVERSE)."""

    __tablename__ = "change_log"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id"))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
