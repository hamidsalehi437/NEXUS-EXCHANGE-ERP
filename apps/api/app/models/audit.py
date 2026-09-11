"""Audit-trail mapping (PART 18, PART 42).

Table: ``audit_logs``.

The table is append-only (UPDATE/DELETE are refused by triggers *and* by the
application role's privileges) and tamper-evident: ``prev_hash``/``chain_hash``
form a SHA-256 chain over every row, so a silent edit or deletion is detectable
with ``verify_audit_chain()``. ``seq`` is database-generated.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import CHAR, BigInteger, DateTime, FetchedValue, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class AuditLog(UUIDPrimaryKeyMixin, Base):
    """One recorded action with its before/after snapshot."""

    __tablename__ = "audit_logs"

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column()
    old_data: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    new_data: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # BIGSERIAL in the schema: the value is always produced by the database, so the
    # ORM never sends one and reads it back (FetchedValue).
    seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True, server_default=FetchedValue()
    )
    prev_hash: Mapped[str | None] = mapped_column(CHAR(64))
    chain_hash: Mapped[str | None] = mapped_column(CHAR(64))
    request_id: Mapped[str | None] = mapped_column(String(100))
