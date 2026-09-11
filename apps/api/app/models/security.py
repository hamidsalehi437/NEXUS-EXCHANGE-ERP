"""Session-security mapping (PART 24, PART 40, PART 42).

Tables: ``refresh_tokens``, ``idempotency_keys``.

Only the SHA-256 hash of a refresh token is persisted; the token itself never
touches the database or the logs.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    CHAR,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class RefreshToken(UUIDPrimaryKeyMixin, Base):
    """A refresh token inside a rotation family (reuse detection, PART 42)."""

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    family_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )
    token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    issued_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(100))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)


class IdempotencyKey(UUIDPrimaryKeyMixin, Base):
    """Stored answer for an ``Idempotency-Key`` (PART 40)."""

    __tablename__ = "idempotency_keys"

    key: Mapped[uuid.UUID] = mapped_column(nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    endpoint: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'IN_PROGRESS'")
    )
    response_status: Mapped[int | None] = mapped_column(SmallInteger)
    response_body: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    resource_type: Mapped[str | None] = mapped_column(String(100))
    resource_id: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # A retry of the same request by the same user on the same endpoint must find
        # the stored answer (PART 40), not create a second one.
        UniqueConstraint("user_id", "endpoint", "key", name="ux_idempotency_keys_scope"),
    )
