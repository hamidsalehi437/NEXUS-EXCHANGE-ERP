"""Device mapping (PART 8, PART 42).

Table: ``devices``. A device is a registered installation; revocation
(``revoked_at``/``is_active``) invalidates its sessions immediately and is
checked on every API call and sync push.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class Device(UUIDPrimaryKeyMixin, Base):
    """Registered client installation bound to a branch."""

    __tablename__ = "devices"

    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), nullable=False)
    device_uuid: Mapped[uuid.UUID] = mapped_column(nullable=False, unique=True)
    device_name: Mapped[str] = mapped_column(String(200), nullable=False)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    registered_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    app_version: Mapped[str | None] = mapped_column(String(50))
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    revoke_reason: Mapped[str | None] = mapped_column(Text)
