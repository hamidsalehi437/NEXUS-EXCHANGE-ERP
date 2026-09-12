"""Offline-synchronisation mapping (PART 19, PART 34, PART 37).

Tables: ``sync_events``, ``sync_conflicts``, ``sync_cursors``,
``allocation_policies``, ``device_allocations``.

``sync_events`` is append-only in its envelope (identity and payload are frozen)
and only its processing state may advance, which is what makes replaying a batch
idempotent. ``device_allocations`` bound how much a device may post while offline;
consumption may never exceed the granted amount.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class SyncEvent(UUIDPrimaryKeyMixin, Base):
    """An offline event pushed by a device; ``event_id`` is globally unique."""

    __tablename__ = "sync_events"

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(nullable=False, unique=True)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    client_timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    server_timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("'PENDING'")
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    attempt_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    batch_id: Mapped[uuid.UUID | None] = mapped_column()
    idempotency_key: Mapped[uuid.UUID | None] = mapped_column()


class SyncConflict(UUIDPrimaryKeyMixin, Base):
    """A detected conflict awaiting a decision. Never merged automatically."""

    __tablename__ = "sync_conflicts"

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"), nullable=False)
    sync_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sync_events.id"))
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    conflict_type: Mapped[str] = mapped_column(String(50), nullable=False)
    server_version: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    client_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    resolution: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("'PENDING'")
    )
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SyncCursor(Base):
    """Per-device position in the server change stream."""

    __tablename__ = "sync_cursors"

    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    # BIGINT: the server change stream is append-only and must not overflow after
    # years of trading (mirrors ck_sync_cursors_seq, last_seq >= 0).
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AllocationPolicy(UUIDPrimaryKeyMixin, Base):
    """Template for the offline allowance granted to a device in a currency."""

    __tablename__ = "allocation_policies"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id"))
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    currency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("currencies.id"), nullable=False)
    max_amount: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    max_offline_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("480")
    )
    allow_buy: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    allow_sell: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    max_commission: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class DeviceAllocation(UUIDPrimaryKeyMixin, Base):
    """A concrete allowance granted to one device for one currency and window."""

    __tablename__ = "device_allocations"
    __table_args__ = (
        UniqueConstraint(
            "device_id", "currency_id", "window_start", name="ux_device_allocations_window"
        ),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"), nullable=False)
    policy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("allocation_policies.id"))
    currency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("currencies.id"), nullable=False)
    window_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    granted_amount: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    consumed_amount: Mapped[Decimal] = mapped_column(
        Numeric(30, 10), nullable=False, server_default=text("0")
    )
    released_amount: Mapped[Decimal] = mapped_column(
        Numeric(30, 10), nullable=False, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'ACTIVE'"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
